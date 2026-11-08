# TFE · Solución de BI e IA para una pyme peruana de distribución de material eléctrico

Código del Trabajo Fin de Estudios del Máster Universitario en Análisis y Visualización de Datos Masivos (UNIR).
Autor del código: Rubén Domínguez Franco.

Contiene la ingeniería de datos (auditoría de calidad, limpieza y almacén en estrella) y los análisis de
aprendizaje automático descritos en el capítulo 5 de la memoria.

## Datos

Los datos proceden del sistema de facturación de la empresa colaboradora y contienen información comercial
confidencial y datos personales. Por eso no se incluyen en el repositorio (Ley 29733 y Anexo A de la memoria).
Solo se publican metadatos sin datos de la empresa: `docs/diccionario_datos.csv` y `scripts/catalogo_indicadores.csv`.

## Estructura

| Ruta | Contenido |
|---|---|
| `scripts/auditoria.py` | Inventario de ficheros y 55 comprobaciones de calidad |
| `scripts/limpieza.py` | Nueve reglas de limpieza: capa plata, registro de cambios y conciliación |
| `scripts/oro.py` | Modelo en estrella en DuckDB (+ Parquet y CSV), diccionario, seudonimización y conciliación |
| `scripts/vistas_oro.sql` | Ocho vistas que calculan los indicadores del catálogo |
| `scripts/catalogo_indicadores.csv` | Catálogo de los 19 indicadores |
| `cuaderno_arranque.ipynb` | Conexión al almacén y datos de partida |
| `ml/00` a `ml/05` | Cuadernos de análisis: previsión, conversión, riesgo de cobro, segmentación y reglas de asociación |
| `ml/comun.py` | Conexión al almacén y funciones compartidas |

## Reproducción

Con los ficheros de origen en `datos_recibidos/` y el tipo de cambio en `fuentes_externas/`:

```bash
pip install -r requirements.txt
python scripts/auditoria.py datos_recibidos auditoria/resultados_datos_recibidos
python scripts/limpieza.py  datos_recibidos plata
python scripts/auditoria.py plata auditoria/resultados_plata
python scripts/oro.py plata fuentes_externas/tipo_cambio.csv oro
```

Después, abrir los cuadernos de `ml/` (con la carpeta `oro/` al lado) y ejecutarlos en orden.
Semilla fija 20261101: con el mismo almacén se obtienen las mismas cifras. La capa oro solo la puede
regenerar quien custodia la clave de seudonimización, que no forma parte del repositorio.
