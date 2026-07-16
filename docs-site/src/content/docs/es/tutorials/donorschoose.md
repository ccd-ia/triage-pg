---
title: "DonorsChoose — riesgo de financiamiento y deep feature synthesis"
description: ¿Se quedará sin financiamiento un proyecto de aula? Un caso de alerta temprana con señal difusa, y el tutorial para grafos de entidades, disciplina ante el leakage y estrategias de grupos de features.
sidebar:
  order: 4
  label: DonorsChoose (DFS profundo)
---

Los primeros tres tutoriales tenían una ventaja secreta: señal fuerte. El
`sr_type` de Chicago 311 prácticamente *es* la respuesta (AUC ≈ 0.9).
DonorsChoose (KDD Cup 2014) es el opuesto honesto — **señal difusa** repartida
entre muchas features débiles, con los mejores AUCs en los 0.70 — lo que lo
vuelve el dataset correcto para las preguntas que enseña esta página: ¿cómo
*construyes* features a partir de un grafo de entidades multi-flujo, y cómo
averiguas **qué familia de features realmente aporta el lift**?

Prerrequisitos: la [prueba de humo](/triage-pg/es/tutorials/dirtyduckling/); el
vocabulario de encuadre de [DirtyDuck](/triage-pg/es/tutorials/dirtyduck/).

## El caso

Los maestros publican proyectos de aula — libros, microscopios, excursiones —
con un precio; los donantes los financian. Algunos proyectos alcanzan su meta en
días; cerca de un tercio nunca se financia por completo. Saber *al momento de
publicar* qué proyectos van a batallar le permite a la plataforma intervenir
temprano: destacándolos, ofreciendo donaciones de contrapartida, asesorando
sobre la petición.

**La pregunta**: *¿seguirá sin financiamiento este proyecto recién publicado
dentro de cuatro meses?* (La clase positiva es el proyecto que necesita ayuda.)

```yaml
task_framing: early_warning   # funding outcomes are recorded for every project
```

## Los datos — y la trampa de leakage que tienden

```bash
just donors-up          # ~3,000 real projects (2012–13) baked in; full Kaggle data mountable
uv run triage --dbfile donorschoose-database.yaml db upgrade
```

La capa `ontology` es un grafo de cuatro entidades alrededor de los
**proyectos**:

- **`ontology.entities` = proyectos** — el objetivo; atributos estáticos (nivel
  de grado, materia, nivel de pobreza, precio) conocidos al publicar;
- **resources** — los renglones de la petición (¿libros? ¿tecnología? cuántos,
  a qué precio) — conocidos al publicar, un flujo hijo legítimo;
- **teacher history / school history** — proyectos *previos* del mismo maestro o
  escuela, alcanzados de forma autorreferencial mediante
  `teacher_acctid` / `schoolid`;
- **donations** — **únicamente la fuente de la etiqueta**. Nunca una feature. Al
  momento de publicar, un proyecto tiene cero donaciones por definición;
  cualquier feature derivada de donaciones es puro leakage disfrazado de señal.
  La config nunca referencia la tabla de donations en `feature_config`, y esa
  ausencia es una afirmación de diseño, no un descuido.

La etiqueta compara cuatro meses de donaciones contra `total_price`:

```sql
(coalesce(sum(donations within {label_timespan}), 0) < total_price)::int
```

## Features — un grafo de entidades real, todo as-of

Este es el `feature_config` más profundo de los tutoriales — un objetivo con
**tres flujos hijos**, cada uno unido as-of para que solo cuente lo que existía
antes del `as_of_date`:

- `projects.*` — categóricas one-hot + numéricas de la petición misma;
- `resources.*` — agregaciones sobre los renglones (conteos, estadísticas de
  precio, mezcla de tipos);
- `teacher_history.*` — los proyectos *previos* del mismo maestro: cuántos, con
  qué frecuencia se financiaron, precio típico. Un maestro primerizo no tiene
  filas — lo cual es en sí mismo información, manejada por la regla de
  imputación, no por espiar;
- `school_history.*` — lo mismo, a la granularidad de la escuela.

featurizer expande esto a ~30 features a lo largo de las cuatro familias. Las
historias son las sutiles: "la tasa de financiamiento pasada del maestro" se
calcula *a la fecha* (as of) de cada fecha de publicación a partir de los
proyectos publicados estrictamente antes — un join as-of autorreferencial que
tendrías que escribir a mano con mucho cuidado en SQL puro, y que harías mal sin
notarlo la primera vez.

## Ejecútalo — luego pregunta qué familia importa

```bash
uv run triage --dbfile donorschoose-database.yaml run \
  example/donorschoose/experiment.yaml --project-path /tmp/donors-run
```

(5 grupos de modelos × 4 splits = 20 modelos sobre el subconjunto incluido; tasa
base ≈ 0.32.)

Ahora el capítulo por el que existe este dataset. Descomenta el bloque
`feature_groups` en la config (viene comentado, dentro de `feature_config`):

```yaml
feature_config:
  # …the entity graph…
  feature_groups:
    group_by: source_entity
    strategies: [all, leave-one-out]
```

y vuelve a ejecutar. **Un experimento se abre en abanico en cinco runs** — el
hash del problema no cambia (las features son el *intento*, no el *problema*),
así que sus leaderboards son directamente comparables:

```text
  run 0cb379da… (all):                            20 model(s)
  run 3f32af45… (leave-one-out:projects):         20 model(s)
  run 49c1d0ce… (leave-one-out:resources):        20 model(s)
  run bf24745c… (leave-one-out:school_history):   20 model(s)
  run d644f9d9… (leave-one-out:teacher_history):  20 model(s)
```

Cada run `leave-one-out:X` entrena sin la familia X. Lee la comparación en la
pestaña **Model Groups** del dashboard (o `triage models <hash>`): si quitar
`teacher_history` apenas mueve la métrica, su lift es redundante con las demás;
si quitar `projects` la desploma, los atributos propios de la petición cargan el
modelo. Sobre el subconjunto incluido las diferencias son pequeñas y ruidosas —
los mejores AUCs se quedan en los 0.70 bajos a medios sin importar qué familia
quites — *y ese es el hallazgo*: los problemas de señal difusa son justo donde
las ablaciones de familias de features te salvan de sobre-narrar la importancia
de cualquier feature individual. (Con los 1.6 GB completos de datos de Kaggle
montados, los contrastes se agudizan.)

La cohorte, las etiquetas y los artefactos de features compartidos hacen
cache-hit a lo largo de los cinco runs — el abanico cuesta tiempo de
entrenamiento marginal, no una reconstrucción del pipeline.

## Leer un leaderboard de señal difusa

![Una ficha de modelo: curvas de umbral, histograma de scores, calibración, importancias](../../../../assets/tutorials/model-sheet.png)

Dos hábitos que premia este dataset:

- **Mira la estabilidad, no la mejor celda individual.** Con señal débil, el
  ganador por split cambia; las reglas de arrepentimiento (regret) de audition
  (`triage audition`) eligen el grupo que *nunca está lejos del mejor*, que es la
  propiedad desplegable.
- **Cuida la tasa base (≈ 0.32).** Precision@k tiene que superarla para
  significar algo; un AUC de 0.72 aquí es trabajo honesto, no un resultado débil
  — compara el caso de inspecciones de DirtyDuck (base 0.277, lift moderado) y la
  señal estructural de 311 (0.87+). Tres datasets, tres regímenes de señal: esa
  calibración de expectativas es el verdadero entregable de esta serie.

## En qué difiere esto de DSSG triage

Las apariciones de DonorsChoose en DSSG (baselines de la era KDD) construían a
mano las features agregadas; aquí el grafo de cuatro familias son nueve líneas de
YAML por familia y el estudio de ablación son dos. Las *estrategias* de grupos de
features también existían en DSSG triage — triage-pg conserva la idea pero hace
de cada subconjunto un **run** de primera clase del mismo experimento, así que la
procedencia, el caché y el leaderboard tratan la ablación como datos, no como
cinco experimentos separados que contabilizar. El
[comparativo](https://ccd-ia.github.io/triage-pg/triage-pg-vs-dssg-triage.html)
tiene el resto.

## Qué sigue

Ya viste los tres regímenes de señal y ambos regímenes de observación. Desde
aquí:

- [`docs/quickstart.md`](https://github.com/ccd-ia/triage-pg/blob/main/docs/quickstart.md)
  §"your own data" — apuntar triage-pg a tu propio PostgreSQL;
- el [one-pager de onboarding](https://ccd-ia.github.io/triage-pg/onboarding.html)
  como el mapa de todo lo demás;
- `just donors-down` cuando termines.
