---
title: "Chicago 311 — alerta temprana, con forma de producción"
description: Un sistema de alerta temprana sobre solicitudes de servicio, llevado a las superficies que el tutorial de DSSG nunca tuvo —auditoría de justicia (fairness), evaluaciones por subconjuntos, monitoreo y análisis de supervivencia.
sidebar:
  order: 3
  label: Chicago 311 (EWS + producción)
---

DirtyDuck enseñó la lección del encuadre del problema (problem framing). Este
tutorial toma el *otro* régimen —un verdadero **sistema de alerta temprana**
(early warning system), donde el desenlace se observa para cada miembro de la
cohorte— y lo lleva por las superficies que necesitarías para operar un modelo
así de verdad: auditoría de justicia (fairness), evaluaciones por subconjuntos,
monitoreo a lo largo del tiempo y una reformulación de supervivencia. Ninguna
de estas existía en el tutorial de DSSG triage; aquí todas son un bloque de
configuración o un comando de la CLI.

Requisitos previos: la [prueba de humo](/triage-pg/es/tutorials/dirtyduckling/)
pasa y, idealmente, ya leíste [DirtyDuck](/triage-pg/es/tutorials/dirtyduck/).

## El caso

La línea 311 de Chicago recibe solicitudes de servicio: baches, grafiti,
luminarias descompuestas, quejas de saneamiento. Algunas se resuelven el mismo
día; otras quedan pendientes durante meses. Una solicitud que se va a resolver
con lentitud vale la pena conocerla *en el momento en que se presenta*: se
puede escalar, reasignar o, como mínimo, comunicar con honestidad ("solicitudes
como la suya tardan actualmente ~5 semanas").

**La pregunta**: *¿qué solicitudes, en el momento en que se presentan, tardarán
más de 14 días en resolverse?*

## Los datos y el stack

```bash
just chi311-up          # 30,654 real service requests from 2019, baked into the image
uv run triage --dbfile chicago311-database.yaml db upgrade
```

La misma forma de tres capas que todo proyecto triage-pg: `raw` → `clean` →
`ontology.entities` (un renglón por solicitud: `sr_type`, `owner_department`,
`origin`, `ward`, `community_area`, `created_date`, `closed_date`) y
`ontology.events`. La **entidad aquí es la solicitud misma**, no una
instalación —un contraste deliberado con DirtyDuck: las cohortes no tienen que
ser "cosas con historia"; pueden ser *eventos en su momento de creación*.

## Formulación — y por qué el %-etiquetado es 100 esta vez

Desde [`example/chicago311/experiment.yaml`](https://github.com/ccd-ia/triage-pg/blob/main/example/chicago311/experiment.yaml):
la cohorte en cada `as_of_date` mensual es cada solicitud presentada el mes
anterior; la etiqueta (label) es *resolución lenta*:

```sql
select
  e.entity_id,
  (e.closed_date is null
   or e.closed_date >= e.created_date + {label_timespan})::int as outcome
from ontology.entities as e
where e.created_date >= {as_of_date}::date - interval '1 month'
  and e.created_date <  {as_of_date}::date
```

Que una solicitud se haya resuelto es un **hecho administrativo**: los propios
registros de la ciudad terminan cerrando cada ticket, así que `closed_date` (o
su ausencia) es conocible para toda la cohorte una vez que la ventana madura.
Nadie tiene que ser "inspeccionado" para que el desenlace exista. Ese es el
**régimen de alerta temprana**:

```yaml
task_framing: early_warning
```

y es por eso que la tarjeta de %-etiquetado del dashboard marca **100%** aquí
sin ninguna alarma —mientras que la configuración de inspecciones de DirtyDuck
se queda en ~54%. Si alguna vez ves un experimento de alerta temprana *por
debajo* de 100%, la tarjeta ahora te advierte: algo anda mal con la consulta de
la etiqueta o con los datos, porque este régimen promete observación completa.
(Esta pregunta exacta —"¿cómo puede un proyecto de estilo inspecciones estar
100% etiquetado?"— es de donde nació el tag de encuadre `task_framing`.)

Tasa base (base rate): **≈ 21%** de las solicitudes son lentas. Y una nota de
honestidad pedagógica: la mayor parte de la señal vive en `sr_type` —los baches
son estructuralmente lentos (~73% lentos, mediana de 37 días), el grafiti se
resuelve el mismo día. Un modelo honesto alcanza un AUC ≈ 0.87 con cero fuga
(leakage); la información de resolución nunca es una característica (feature).

## Características — atributos de la solicitud + presión del backlog

Dos familias de características, ambas correctas en su as-of:

- **los atributos propios de la solicitud**: `sr_type` / `owner_department` /
  `origin` en codificación one-hot, `ward` numérico y hora de presentación;
- **agregaciones de backlog** —el estado del sistema cuando presentaste:
  `area_backlog` (volumen reciente de solicitudes en tu área comunitaria) y
  `type_demand` (demanda reciente de tu tipo de servicio), agregadas sobre
  ventanas retrospectivas.

La segunda familia es la interesante: hace que el modelo sea *operativo* —"tu
bache será lento *porque el sistema está ahogado en baches ahora mismo*", no
solo "los baches son lentos".

## Ejecútalo

La configuración del tutorial con toda la superficie de producción —fairness +
subconjuntos + encuadre— es la configuración base más tres bloques neutrales en
identidad (que se muestran en las secciones de abajo):

```bash
uv run triage --dbfile chicago311-database.yaml run \
  example/chicago311/experiment.yaml --project-path /tmp/chi311-run
uv run triage --dbfile chicago311-database.yaml leaderboard <hash-prefix>
just serve 8001        # cp chicago311-database.yaml database.yaml first
```

Espera 5 grupos de modelos × 4 splits = 20 modelos, ~58,000 predicciones y un
leaderboard cuyos AUC más altos se ubican entre los .80 altos y los .90 bajos
por split.

## Fairness — la geografía como atributo protegido

La capacidad de respuesta del 311 tiene una larga historia de derechos civiles:
los tiempos de respuesta que difieren por vecindario son diferencias en los
problemas de quién se resuelven. El proxy honesto de atributo protegido en
estos datos es la **geografía**: `community_area`:

```yaml
bias_config:
  query: |
    select entity_id, community_area
    from ontology.entities
    where created_date < '{as_of_date}'
  parameter: 300_abs
  tau: 0.8
```

Neutral en identidad: agregarlo y volver a ejecutar hace cache-hit en todo el
pipeline y *suma* la auditoría —cientos de miles de renglones de atributo
protegido y métricas de fairness por área sobre la lista de los top-300. En la
**pestaña Bias** del dashboard: ocho métricas por grupo con razones de
disparidad y veredictos-τ (un grupo cuya disparidad cae fuera de [τ, 1/τ]
falla), y el **asistente del árbol de fairness (fairness-tree wizard)** —el
árbol de decisión Aequitas de DSSG como guía interactiva. Dos preguntas ("¿la
intervención es punitiva o asistencial?", "¿intervienes sobre todos los
marcados?") resaltan *cuál* familia de métricas debería importarte —aquí una
escalación es asistencial, así que la paridad de *falsos negativos* (a quién se
le pasa por alto) importa más que a quién se escala por error.

## Subconjuntos — evalúa donde aplica la política

Las métricas de toda la ciudad pueden ocultar fallas a nivel de vecindario. Una
evaluación por subconjunto vuelve a rankear y vuelve a evaluar *dentro* de una
rebanada con nombre:

```yaml
evaluation:
  subsets:
    - name: austin
      query: |
        select entity_id from ontology.entities
        where community_area = 25 and created_date < '{as_of_date}'
```

Los paneles de evaluación del dashboard ganan un selector de población (cohorte
completa ↔ austin), y el leaderboard de la CLI acepta la misma opción. La
semántica importa: el subconjunto se vuelve a rankear **dentro de sí mismo**
—precision@300 entre las solicitudes de Austin, como si Austin fuera todo tu
mundo— que es la pregunta que un coordinador de área realmente hace.

## Monitoreo — qué pasa después del backtest

Todo lo anterior es backtesting (`purpose: experiment` —el chip de procedencia
en la vista de monitoreo así lo dice). Producción significa *puntuar (scoring)
hacia adelante según un calendario* y vigilar el deterioro. El monitoreo de
triage-pg es deliberadamente libre de demonios: un punto de entrada de la CLI
programado más vistas SQL sobre el historial de predicciones append-only
(solo-inserción).

```bash
# score a date's cohort with a chosen model (normally a cron/EventBridge job;
# the date defaults to today — with the 2019 tutorial data, use one in range)
uv run triage --dbfile chicago311-database.yaml score <model-id> 2019-12-01
```

Cada invocación *agrega* predicciones marcadas con `scored_at` (el momento de
la puntuación) —nunca sobrescribe— así que día tras día el historial se acumula
y la **vista de Monitoreo** se va llenando:

- **drift de score (deriva)**: PSI y KS entre la ventana de referencia y los
  scores más recientes (umbrales marcados con chips verde/ámbar/rojo);
- **volumen**: predicciones por día de puntuación —el latido que te dice que el
  cron está vivo;
- **desenlaces realizados**: conforme las etiquetas maduran, volver a ejecutar
  la evaluación hace upsert de las métricas realizadas por as-of date —la curva
  de "¿el modelo seguía teniendo razón?".

![La vista de monitoreo: chips de drift de score, latido de volumen de puntuación, desenlaces realizados](../../../../assets/tutorials/monitoring-view.png)

Append-only es la decisión de diseño que hace que todo esto sea barato
(ADR-0006): un score nunca es "el" score, es un renglón con una marca de
tiempo; "actual" es solo `max(scored_at)`.

## Supervivencia — misma pregunta, respuesta más fina

"Lento o no lento" tira información: ¿*qué tan* lento? La variante de
supervivencia ([`example/chicago311/experiment-survival.yaml`](https://github.com/ccd-ia/triage-pg/blob/main/example/chicago311/experiment-survival.yaml))
reformula la etiqueta como tiempo-hasta-resolución:

```yaml
problem_type: survival
# label produces: duration (days filing → closure), event_observed
# (false = still open at the window's end — censored, not ignored)
```

La censura es el meollo: una solicitud aún abierta cuando la ventana se cierra
no es una etiqueta faltante, es una *cota inferior* ("al menos 60 días"). Los
estimadores de supervivencia (el modelo de Cox de scikit-survival detrás del
extra `survival`) usan los renglones censurados correctamente, y la evaluación
cambia al **índice de concordancia** —calculado por una función PL/pgSQL dentro
de la base de datos, coincidiendo con la referencia de scikit-survival hasta
1e-9. En el dashboard, el encabezado del experimento de supervivencia muestra
la píldora `survival`, renglones de duración/censurado en el cajón de la
entidad (entity drawer), y una tarjeta de **tasa de eventos (event rate)** donde
la clasificación muestra una tasa base.

```bash
uv run triage --dbfile chicago311-database.yaml run \
  example/chicago311/experiment-survival.yaml --project-path /tmp/chi311-run
```

## En qué difiere esto de DSSG triage

Aquí el fairness es SQL sobre una tabla `protected_groups` en formato largo
(las métricas de Aequitas, nada de su runtime); los subconjuntos se vuelven a
rankear en la base de datos; el monitoreo es CLI + vistas SQL en lugar de un
producto de calendarización externo; la supervivencia es un `problem_type` de
primera clase en vez de estar fuera de alcance. El
[comparativo lado a lado](https://ccd-ia.github.io/triage-pg/triage-pg-vs-dssg-triage.html)
tiene el recuento completo.

## Hacia dónde seguir

- [**DonorsChoose**](/triage-pg/es/tutorials/donorschoose/) — señal difusa y la
  vitrina de deep feature synthesis.
- [`docs/fairness.md`](https://github.com/ccd-ia/triage-pg/blob/main/docs/fairness.md) ·
  [`docs/monitoring.md`](https://github.com/ccd-ia/triage-pg/blob/main/docs/monitoring.md) ·
  [`docs/problem-types.md`](https://github.com/ccd-ia/triage-pg/blob/main/docs/problem-types.md)
- `just chi311-down` al terminar.
