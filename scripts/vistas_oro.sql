-- Vistas de indicadores sobre el modelo en estrella (DuckDB).
-- Cada vista responde a un indicador del catalogo; el cuadro de mando y el asistente de
-- consulta parten de ellas en lugar de repetir la logica en cada informe.

-- VEN-01 a VEN-05, MAR-01 y MAR-02: ventas, margen, ticket medio, clientes y notas de credito por mes
-- (en soles, sin IGV). CLIENTES VARIOS (ventas de mostrador sin identificar) no cuenta como cliente.
CREATE OR REPLACE VIEW v_ventas_mensuales AS
WITH base AS (
    SELECT f.anio_mes,
           SUM(v.importe_neto_pen)                                              AS ventas_netas,
           SUM(v.coste_pen)                                                     AS coste,
           SUM(v.margen_pen)                                                    AS margen,
           SUM(CASE WHEN td.signo = 1 THEN v.importe_neto_pen ELSE 0 END)       AS ventas_brutas,
           -SUM(CASE WHEN td.signo = -1 THEN v.importe_neto_pen ELSE 0 END)     AS notas_credito,
           COUNT(DISTINCT CASE WHEN td.signo = 1 THEN v.serie || '-' || v.numero END) AS n_comprobantes,
           COUNT(DISTINCT CASE WHEN td.signo = 1 AND c.cliente_id <> '00000000' THEN c.cliente_id END) AS n_clientes
    FROM fact_ventas v
    JOIN dim_fecha f           ON f.fecha_key = v.fecha_emision_key
    JOIN dim_tipo_documento td ON td.tipo_documento_key = v.tipo_documento_key
    JOIN dim_cliente c         ON c.cliente_key = v.cliente_key
    GROUP BY f.anio_mes)
SELECT anio_mes,
       ROUND(ventas_netas, 2)                                                  AS ventas_netas_pen,
       ROUND(100 * (ventas_netas / NULLIF(LAG(ventas_netas, 12) OVER (ORDER BY anio_mes), 0) - 1), 1) AS crecimiento_interanual_pct,
       ROUND(coste, 2)                                                         AS coste_pen,
       ROUND(margen, 2)                                                        AS margen_pen,
       ROUND(100 * margen / NULLIF(ventas_netas, 0), 1)                        AS margen_pct,
       n_comprobantes,
       n_clientes,
       ROUND(ventas_brutas / NULLIF(n_comprobantes, 0), 2)                     AS ticket_medio_pen,
       ROUND(notas_credito, 2)                                                 AS notas_credito_pen,
       ROUND(100 * notas_credito / NULLIF(ventas_brutas, 0), 2)                AS notas_credito_pct
FROM base
ORDER BY anio_mes;

-- MAR-03: margen por familia y linea de negocio, por ano.
CREATE OR REPLACE VIEW v_margen_familia AS
SELECT f.anio, p.linea_negocio, p.familia,
       ROUND(SUM(v.importe_neto_pen), 2)                                       AS ventas_netas_pen,
       ROUND(SUM(v.margen_pen), 2)                                             AS margen_pen,
       ROUND(100 * SUM(v.margen_pen) / NULLIF(SUM(v.importe_neto_pen), 0), 1)  AS margen_pct
FROM fact_ventas v
JOIN dim_fecha f    ON f.fecha_key = v.fecha_emision_key
JOIN dim_producto p ON p.producto_key = v.producto_key
GROUP BY ALL
ORDER BY f.anio, ventas_netas_pen DESC;

-- VEN-06: concentracion de clientes, por ano: parte de las ventas que hace el 20 % de clientes que mas compra.
CREATE OR REPLACE VIEW v_concentracion_clientes AS
WITH por_cliente AS (
    SELECT f.anio, c.cliente_id, SUM(v.importe_neto_pen) AS ventas
    FROM fact_ventas v
    JOIN dim_fecha f   ON f.fecha_key = v.fecha_emision_key
    JOIN dim_cliente c ON c.cliente_key = v.cliente_key
    WHERE c.cliente_id <> '00000000'
    GROUP BY ALL),
ordenado AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY anio ORDER BY ventas DESC) AS puesto,
           COUNT(*) OVER (PARTITION BY anio) AS n_clientes
    FROM por_cliente)
SELECT anio, MAX(n_clientes) AS n_clientes,
       CAST(CEIL(MAX(n_clientes) * 0.2) AS INTEGER) AS n_clientes_top20,
       ROUND(100 * SUM(CASE WHEN puesto <= CEIL(n_clientes * 0.2) THEN ventas ELSE 0 END) / SUM(ventas), 1) AS pct_ventas_top20
FROM ordenado
GROUP BY anio
ORDER BY anio;

-- VEN-07: clasificacion ABC de productos por ventas netas (A = 80 % de las ventas, B = siguiente 15 %).
CREATE OR REPLACE VIEW v_productos_abc AS
WITH ventas AS (
    SELECT p.codigo_producto, p.descripcion, p.familia,
           SUM(v.importe_neto_pen) AS ventas_netas_pen, SUM(v.margen_pen) AS margen_pen, SUM(v.cantidad) AS unidades
    FROM fact_ventas v JOIN dim_producto p ON p.producto_key = v.producto_key
    GROUP BY ALL),
acum AS (
    SELECT *, SUM(ventas_netas_pen) OVER (ORDER BY ventas_netas_pen DESC) / SUM(ventas_netas_pen) OVER () AS pct_acumulado
    FROM ventas)
SELECT codigo_producto, descripcion, familia, ROUND(ventas_netas_pen, 2) AS ventas_netas_pen, ROUND(margen_pen, 2) AS margen_pen,
       unidades, ROUND(100 * pct_acumulado, 1) AS pct_acumulado,
       CASE WHEN pct_acumulado <= 0.80 THEN 'A' WHEN pct_acumulado <= 0.95 THEN 'B' ELSE 'C' END AS clase_abc
FROM acum
ORDER BY ventas_netas_pen DESC;

-- COM-01 a COM-05: embudo comercial por mes de emision de la proforma.
CREATE OR REPLACE VIEW v_embudo_mensual AS
SELECT f.anio_mes,
       COUNT(*)                                                       AS proformas,
       CAST(SUM(e.convertida) AS INTEGER)                             AS convertidas,
       CAST(SUM(CASE WHEN ep.estado = 'RECHAZADA' THEN 1 ELSE 0 END) AS INTEGER) AS rechazadas,
       CAST(SUM(CASE WHEN ep.estado = 'VENCIDA' THEN 1 ELSE 0 END) AS INTEGER)   AS vencidas,
       CAST(SUM(CASE WHEN ep.estado = 'ABIERTA' THEN 1 ELSE 0 END) AS INTEGER)   AS abiertas,
       ROUND(100 * SUM(e.convertida) / NULLIF(SUM(CASE WHEN ep.es_resuelta THEN 1 ELSE 0 END), 0), 1) AS tasa_conversion_pct,
       MEDIAN(e.dias_hasta_venta)                                     AS dias_mediana_hasta_venta,
       ROUND(SUM(e.importe_ofertado_pen), 2)                          AS ofertado_pen,
       ROUND(SUM(e.importe_facturado_pen), 2)                         AS facturado_pen,
       ROUND(100 * SUM(CASE WHEN ep.estado = 'RECHAZADA' THEN 1 ELSE 0 END)
             / NULLIF(SUM(CASE WHEN ep.es_resuelta THEN 1 ELSE 0 END), 0), 1) AS tasa_rechazo_pct,
       ROUND(100 * SUM(e.importe_facturado_pen) / NULLIF(SUM(e.importe_ofertado_pen), 0), 1) AS facturado_sobre_ofertado_pct
FROM fact_embudo_proforma e
JOIN dim_fecha f            ON f.fecha_key = e.fecha_emision_key
JOIN dim_estado_proforma ep ON ep.estado_proforma_key = e.estado_proforma_key
GROUP BY f.anio_mes
ORDER BY f.anio_mes;

-- COB-03 y COB-04: cartera pendiente al cierre de los datos, por comprobante y tramo de antiguedad.
-- Saldo = total con IGV - notas de credito que lo corrigen - cobros, detracciones y retenciones.
CREATE OR REPLACE VIEW v_cartera_pendiente AS
WITH corte AS (SELECT MAX(fecha_emision_key) AS k FROM fact_ventas),
docs AS (
    SELECT v.serie || '-' || v.numero AS documento, ANY_VALUE(v.cliente_key) AS cliente_key,
           ANY_VALUE(v.moneda_key) AS moneda_key, ANY_VALUE(v.fecha_emision_key) AS fecha_emision_key,
           ANY_VALUE(v.fecha_vencimiento_key) AS fecha_vencimiento_key, ANY_VALUE(v.tipo_cambio) AS tipo_cambio,
           SUM(v.importe_neto_doc + v.igv_doc) AS total_doc
    FROM fact_ventas v JOIN dim_tipo_documento td ON td.tipo_documento_key = v.tipo_documento_key
    WHERE td.signo = 1
    GROUP BY 1),
nc AS (
    SELECT documento_referencia AS documento, -SUM(importe_neto_doc + igv_doc) AS nc_doc
    FROM fact_ventas WHERE documento_referencia <> '' GROUP BY 1),
cobrado AS (
    SELECT serie_doc || '-' || numero_doc AS documento, SUM(importe_doc) AS cobrado_doc FROM fact_cobros GROUP BY 1)
SELECT d.documento, c.cliente_id, c.nombre, m.codigo_iso AS moneda, fe.fecha AS fecha_emision, fv.fecha AS fecha_vencimiento,
       ROUND(d.total_doc, 2) AS total_doc, ROUND(COALESCE(nc.nc_doc, 0), 2) AS notas_credito_doc,
       ROUND(COALESCE(cb.cobrado_doc, 0), 2) AS cobrado_doc,
       ROUND(d.total_doc - COALESCE(nc.nc_doc, 0) - COALESCE(cb.cobrado_doc, 0), 2) AS saldo_doc,
       ROUND((d.total_doc - COALESCE(nc.nc_doc, 0) - COALESCE(cb.cobrado_doc, 0)) * d.tipo_cambio, 2) AS saldo_pen,
       DATE_DIFF('day', CAST(fv.fecha AS DATE), CAST(fcorte.fecha AS DATE)) AS dias_vencido,
       CASE WHEN CAST(fv.fecha AS DATE) >= CAST(fcorte.fecha AS DATE) THEN 'NO VENCIDO'
            WHEN DATE_DIFF('day', CAST(fv.fecha AS DATE), CAST(fcorte.fecha AS DATE)) <= 30 THEN '1-30 DIAS'
            WHEN DATE_DIFF('day', CAST(fv.fecha AS DATE), CAST(fcorte.fecha AS DATE)) <= 60 THEN '31-60 DIAS'
            WHEN DATE_DIFF('day', CAST(fv.fecha AS DATE), CAST(fcorte.fecha AS DATE)) <= 90 THEN '61-90 DIAS'
            ELSE 'MAS DE 90 DIAS' END AS tramo_antiguedad
FROM docs d
CROSS JOIN corte
JOIN dim_fecha fcorte ON fcorte.fecha_key = corte.k
JOIN dim_cliente c    ON c.cliente_key = d.cliente_key
JOIN dim_moneda m     ON m.moneda_key = d.moneda_key
JOIN dim_fecha fe     ON fe.fecha_key = d.fecha_emision_key
JOIN dim_fecha fv     ON fv.fecha_key = d.fecha_vencimiento_key
LEFT JOIN nc          ON nc.documento = d.documento
LEFT JOIN cobrado cb  ON cb.documento = d.documento
WHERE d.total_doc - COALESCE(nc.nc_doc, 0) - COALESCE(cb.cobrado_doc, 0) > 0.5;

-- COB-01 y COB-02: comportamiento de cobro de las ventas a credito, por mes del cobro.
CREATE OR REPLACE VIEW v_cobro_mensual AS
SELECT f.anio_mes,
       ROUND(SUM(c.importe_pen), 2)                                                   AS cobrado_pen,
       ROUND(SUM(c.dias_desde_emision * c.importe_pen) / NULLIF(SUM(c.importe_pen), 0), 1) AS dias_medios_cobro,
       ROUND(100 * SUM(CASE WHEN c.dias_sobre_vencimiento <= 0 THEN c.importe_pen END)
             / NULLIF(SUM(c.importe_pen), 0), 1)                                      AS pct_cobrado_en_plazo
FROM fact_cobros c
JOIN dim_fecha f           ON f.fecha_key = c.fecha_cobro_key
JOIN dim_condicion_pago cp ON cp.condicion_pago_key = c.condicion_pago_key
WHERE c.tipo_movimiento = 'COBRO' AND cp.es_credito
GROUP BY f.anio_mes
ORDER BY f.anio_mes;

-- Comparacion entre vendedores por ano (analisis diagnostico): ventas, margen y conversion.
CREATE OR REPLACE VIEW v_vendedores AS
WITH ventas AS (
    SELECT f.anio, vd.vendedor_codigo, SUM(v.importe_neto_pen) AS ventas, SUM(v.margen_pen) AS margen
    FROM fact_ventas v
    JOIN dim_fecha f     ON f.fecha_key = v.fecha_emision_key
    JOIN dim_vendedor vd ON vd.vendedor_key = v.vendedor_key
    GROUP BY ALL),
embudo AS (
    SELECT f.anio, vd.vendedor_codigo,
           COUNT(*) AS proformas,
           100.0 * SUM(e.convertida) / NULLIF(SUM(CASE WHEN ep.es_resuelta THEN 1 ELSE 0 END), 0) AS conversion,
           MEDIAN(e.dias_hasta_venta) AS dias_mediana
    FROM fact_embudo_proforma e
    JOIN dim_fecha f            ON f.fecha_key = e.fecha_emision_key
    JOIN dim_vendedor vd        ON vd.vendedor_key = e.vendedor_key
    JOIN dim_estado_proforma ep ON ep.estado_proforma_key = e.estado_proforma_key
    GROUP BY ALL)
SELECT v.anio, v.vendedor_codigo, ROUND(v.ventas, 2) AS ventas_netas_pen, ROUND(100 * v.margen / NULLIF(v.ventas, 0), 1) AS margen_pct,
       e.proformas, ROUND(e.conversion, 1) AS tasa_conversion_pct, e.dias_mediana AS dias_mediana_hasta_venta
FROM ventas v
LEFT JOIN embudo e USING (anio, vendedor_codigo)
ORDER BY v.anio, v.ventas DESC;
