---
title: "DirtyDuck — el caso de estudio completo"
description: Inspecciones de alimentos de Chicago, de principio a fin — los mismos datos formulados como priorización de recursos y como sistema de alerta temprana, y por qué esa única decisión lo cambia todo.
sidebar:
  order: 2
  label: DirtyDuck (caso completo)
---

Esta es la versión de triage-pg del
[tutorial Dirty Duck](https://dssg.github.io/triage/dirtyduck/) de DSSG triage:
la misma historia de las inspecciones de alimentos de Chicago, contada sobre el
stack greenfield. Su pieza central es la lección alrededor de la cual DSSG
estructuró todo su tutorial — **los mismos datos sustentan dos problemas de
predicción genuinamente distintos**, y la diferencia vive en una sola decisión
de modelado.

Ejecuta primero la [prueba de humo Dirty Duckling](/triage-pg/es/tutorials/dirtyduckling/);
esta página asume que tu stack funciona (la base de datos de alimentos levantada
en 5440, el esquema migrado).

## El caso

Chicago inspecciona establecimientos de alimentos — restaurantes, tiendas de
abarrotes, escuelas, panaderías. Algunas inspecciones encuentran violaciones
críticas ("fail"); la mayoría no. Los inspectores son escasos: solo cerca de la
mitad de los establecimientos activos se inspecciona en cualquier ventana de
seis meses. Dos equipos distintos de la ciudad podrían hacer dos preguntas
distintas al mismo historial de inspecciones:

1. **El equipo de inspecciones**: *"Dado que solo podemos visitar cierta
   cantidad de establecimientos, ¿cuáles — de ser inspeccionados — tienen mayor
   probabilidad de resultar en violación?"*
2. **Un equipo de monitoreo/alerta temprana (early warning)**: *"¿Qué
   establecimientos aparecerán en el registro de inspecciones reprobadas en los
   próximos seis meses?"*

Suenan parecidas. No lo son — y la diferencia es exactamente lo que el chip
`task_framing` en el dashboard hace visible. (La taxonomía completa de ambos
ejes — tipos de problema y regímenes de observación — es la
[referencia del espacio de problemas](/triage-pg/es/reference/problems/).)

## Los datos

`just tutorial-up` te da un PostgreSQL con tres capas (el patrón que sigue todo
proyecto triage-pg):

- `raw.*` — el archivo de inspecciones tal como fue ingerido;
- `clean.*` — tipado, deduplicado;
- `ontology.*` — la capa de modelado: **`ontology.entities`** (una fila por
  establecimiento: tipo, código postal, un daterange `activity_period`) y
  **`ontology.events`** (una fila por inspección: `date`, `result`, `risk`,
  `type`).

Una columna merece ceremonia: `ontology.events.date` es la **fecha de
conocimiento** (knowledge date) de la inspección — cuándo se supo el resultado.
Cada feature calculada a partir de eventos se une *a la fecha* (as of) usando
esta columna, nunca nada posterior. Esa es la regla cardinal del ML temporal:
**las features para un `as_of_date` solo pueden usar lo que era conocible
estrictamente antes de esa fecha.** Equivócate en esto y tu backtest lee el
futuro sin avisar ("leakage"); cada número que reporta se vuelve ficción.

## El problema, formulado dos veces

Ambas formulaciones comparten la cohorte — los establecimientos activos en cada
`as_of_date`:

```sql
select e.entity_id
from ontology.entities as e
where e.activity_period @> {as_of_date}::date
```

y el marco temporal: etiquetas (labels) observadas sobre ventanas de 6 meses, un
modelo reentrenado cada 6 meses, cuatro splits temporales de prueba (2015-07 →
2017-01). Los placeholders `{as_of_date}` y `{label_timespan}` los llena el motor
temporal — escribes el SQL una vez y corre de forma point-in-time-correcta para
cada split temporal.

### Formulación 1 — priorización de recursos (la config versionada)

[`example/dirtyduck/experiment.yaml`](https://github.com/ccd-ia/triage-pg/blob/main/example/dirtyduck/experiment.yaml)
etiqueta un establecimiento a partir de sus inspecciones **dentro de la ventana**:

```sql
select entity_id,
       bool_or(result = 'fail')::integer as outcome
from ontology.events
where {as_of_date}::date <= date
  and date < {as_of_date}::date + {label_timespan}
group by entity_id
```

Un establecimiento sin inspección en la ventana no devuelve **ninguna fila — su
etiqueta es NULL**. No cero: *desconocida*. No miramos. Ese es el **régimen de
priorización de recursos** (`task_framing: resource_prioritization` en la
config), y aparece por todos lados aguas abajo:

- **~54% etiquetado** — la tarjeta de %-etiquetado lleva la nota "selective
  labels — <100% expected" en lugar de una alarma;
- **tasa base 0.277** — *entre los establecimientos inspeccionados*, 28%
  reprueba;
- el entrenamiento y la evaluación usan solo las filas etiquetadas, así que el
  modelo aprende "condicionado a ser el tipo de lugar que se inspecciona…" — con
  todo el sesgo de selección que eso implica. (Las inspecciones no son
  aleatorias: las quejas, los calendarios de riesgo y el historial determinan a
  quién se visita.)

### Formulación 2 — alerta temprana (el gemelo EIS)

[`example/dirtyduck/experiment-eis.yaml`](https://github.com/ccd-ia/triage-pg/blob/main/example/dirtyduck/experiment-eis.yaml)
cambia exactamente una cosa — qué significa "sin inspección":

```sql
select e.entity_id,
       coalesce(bool_or(ev.result = 'fail'), false)::integer as outcome
from ontology.entities as e
left join ontology.events as ev
  on ev.entity_id = e.entity_id
 and {as_of_date}::date <= ev.date
 and ev.date < {as_of_date}::date + {label_timespan}
where e.activity_period @> {as_of_date}::date
group by e.entity_id
```

"¿Aparecerá este establecimiento en el registro de inspecciones reprobadas?" es
conocible para **cada** establecimiento activo — el registro está completo — así
que la ausencia de evento se resuelve a **0** vía coalesce y la etiqueta cubre
toda la cohorte (`task_framing: early_warning`).

Como el SQL de la etiqueta cambió de verdad, este es un **experimento distinto**:
triage-pg hashea el problema (cohorte + etiqueta + config temporal) y las dos
configs obtienen hashes distintos. Corre ambos y compara:

```bash
uv run triage --dbfile dirtyduck-database.yaml run \
  example/dirtyduck/experiment.yaml --project-path /tmp/dirtyduck-run
uv run triage --dbfile dirtyduck-database.yaml run \
  example/dirtyduck/experiment-eis.yaml --project-path /tmp/dirtyduck-run
```

| | priorización de recursos | alerta temprana |
| --- | --- | --- |
| hash del experimento | `b9e38fd8f366…` | `c0d16446f567…` |
| % etiquetado | **53.7%** | **100%** |
| tasa base | **0.277** | **0.116** |
| lo que aprende el modelo | "entre los establecimientos inspeccionados, ¿quién reprueba?" | "¿quién termina en el registro de reprobados?" |
| actuar sobre ello significa | elegir a quién inspeccionar | marcar el riesgo sin importar si alguien habría mirado |

Detente en la línea de la tasa base: 27.7% vs 11.6% *sobre los mismos datos*.
Entre los establecimientos que la ciudad eligió inspeccionar, más de uno de cada
cuatro reprueba; entre todos los establecimientos, uno de cada nueve termina en
el registro. Ninguno de los dos números está mal — responden preguntas
distintas. Publicar uno donde se espera el otro es como los modelos de política
pública engañan. El dashboard mantiene la distinción visible: cada experimento
lleva su píldora de encuadre, y la tarjeta de %-etiquetado se explica en
consecuencia.

Una cosa más que demostró el segundo run: la cohorte y cada feature se
**comparten** entre los dos experimentos. triage-pg direcciona por contenido
(content-addressing) cada artefacto sobre su clausura completa de entradas, así
que el run EIS hizo cache-hit sobre los artefactos de cohorte y de features que
construyó el primer run y solo reconstruyó las etiquetas, las matrices y los
modelos. La pestaña Derivation muestra qué nodos se reutilizaron (marcados como
cache-hit) — la procedencia y el caché son el mismo mecanismo.

## Features — Deep Feature Synthesis, point-in-time

El `feature_config` describe un grafo de entidades, no fórmulas de features: los
establecimientos (el objetivo) con las inspecciones como un flujo de eventos
hijo, relacionados por `entity_id`, unidos **as-of**:

- los atributos del establecimiento se vuelven one-hots de vocabulario fijo
  (`facilities.facility_type=restaurant`, los 15 tipos principales ≈ 96% de las
  entidades);
- el historial de inspecciones se agrega sobre ventanas `P1M`/`P3M`/`P6M` —
  conteos, desgloses por result/risk/type, recencia — cada agregado calculado
  *a la fecha* (as of) de cada fecha usando solo eventos previos.

featurizer (el motor de DFS) expande esto en ~30 features y genera el SQL; nunca
escribes a mano una agregación. Cada feature también necesita una regla de
imputación (aquí: zero-fill fit-free). La división entre imputación
fit-free/fit-based es una frontera de leakage: cualquier cosa *ajustada* (una
media, una mediana) se ajusta **solo sobre el split temporal de entrenamiento** y
se aplica al split temporal de prueba — nunca se calcula sobre la matriz
completa.

## El grid, el run, el leaderboard

El grid versionado es deliberadamente pequeño — dos árboles de decisión, un
random forest, dos regresiones logísticas escaladas (5 grupos × 4 splits = 20
modelos) — porque este tutorial trata sobre el *problema*, no sobre
hiperparámetros. Lee los resultados de tres maneras:

```bash
uv run triage --dbfile dirtyduck-database.yaml leaderboard b9e38fd8   # CLI table
uv run triage --dbfile dirtyduck-database.yaml audition b9e38fd8      # selection rules
just serve 8001                                                       # the dashboard
```

audition es la disciplina de selección de modelos de DSSG calculada en
PostgreSQL: distancia-al-mejor y arrepentimiento (regret) a lo largo de los
splits, para que elijas un grupo de modelos por su *estabilidad a lo largo del
tiempo*, no por un split temporal afortunado. En la ficha del modelo (model
card), la curva de umbral responde la pregunta operativa — "si podemos
inspeccionar el top k, ¿qué precisión/recall obtenemos?" — que es la decisión
real que toma un equipo de inspecciones.

## Equidad y subconjuntos — a un bloque de distancia, neutral a la identidad

Agrega esto a cualquiera de las dos configs (observa el problema, no lo define —
el hash del experimento no cambia):

```yaml
bias_config:
  query: |
    select entity_id, facility_type
    from ontology.entities
    where start_time < '{as_of_date}'
  parameter: 100_abs
  intervention: punitive     # an inspection is a burden → FPR/FDR parity matter

evaluation:
  subsets:
    - name: restaurants
      query: |
        select entity_id from ontology.entities
        where facility_type = 'restaurant' and start_time < '{as_of_date}'
```

Volver a correr con este bloque hace cache-hit sobre todo el pipeline y agrega la
auditoría: métricas de equidad por tipo de establecimiento sobre la lista del
top-100 (17,200 filas de sesgo sobre estos datos — veredictos de disparidad-τ en
la pestaña Bias, con el asistente del árbol de equidad (fairness-tree) explicando
qué familia de métricas implica tu tipo de intervención) y una evaluación
paralela restringida a restaurantes (120 evaluaciones de subconjunto,
re-rankeadas *dentro* del subconjunto). `punitive` importa: cuando la salida del
modelo carga a las personas (inspecciones, auditorías), te importa quién es
*marcado erróneamente* — paridad de falsos positivos — no quién se pasa por alto.

## Los mismos datos, otros objetivos

DirtyDuck funciona también como el escaparate de tipos de problema — cada
variante es una config versionada contra la misma base de datos, ejecutada de la
misma manera:

| Config | `problem_type` | Objetivo |
| --- | --- | --- |
| `experiment.yaml` | classification | reprueba una inspección en 6 meses (régimen de inspecciones) |
| `experiment-eis.yaml` | classification | aparece en el registro de reprobados (régimen de alerta temprana) |
| `experiment-regression.yaml` | regression_ranking | conteo de violaciones sobre la ventana, rankeado |
| `experiment-survival.yaml` | survival | tiempo-hasta-la-falla `(duration, event_observed)`, C-index en PG |
| `experiment-deepgrid.yaml` | classification | un grid más amplio + un gemelo de ablación sin categóricas |
| `experiment-visits.yaml` | classification | régimen **visit-level**: ¿encontrará *esta* inspección una violación? |

## En qué difiere esto de DSSG triage

La discusión de arriba es de DSSG — el encuadre de dos casos es el corazón de su
tutorial Dirty Duck, y el crédito es suyo. Lo que cambió por debajo: la
generación de features pasó del SQL de agregación de collate al grafo de
entidades de featurizer; la evaluación, audition y las métricas de equidad corren
*dentro* de PostgreSQL en lugar de Python + Aequitas; las predicciones son
append-only; cada artefacto está direccionado por contenido (el caché que
viste); y la distinción de encuadre que DSSG enseñaba como narrativa es una clave
de config de primera clase con UI. El recuento completo dimensión por dimensión
es el [comparativo honesto](https://ccd-ia.github.io/triage-pg/triage-pg-vs-dssg-triage.html).

## Qué sigue

- [**Chicago 311**](/triage-pg/es/tutorials/chicago311/) — un caso de alerta
  temprana llevado hasta la auditoría de equidad, el monitoreo y el análisis de
  supervivencia.
- [`docs/fairness.md`](https://github.com/ccd-ia/triage-pg/blob/main/docs/fairness.md),
  [`docs/problem-types.md`](https://github.com/ccd-ia/triage-pg/blob/main/docs/problem-types.md)
  para los tratamientos de referencia.
- `just tutorial-down` cuando termines.
