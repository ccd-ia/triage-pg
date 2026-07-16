---
title: "Dirty Duckling — la prueba de humo"
description: Comprueba que tu instalación de triage-pg funciona, de extremo a extremo, en unos diez minutos — luego haz el recorrido de cinco minutos por el dashboard.
sidebar:
  order: 1
  label: Dirty Duckling (prueba de humo)
---

Esta página hace una sola cosa: **comprobar que tu instalación funciona**. Cada paso
tiene un criterio PASS; si todos se cumplen, tu máquina puede ejecutar todo lo demás en
este sitio. Es el homenaje de triage-pg al
[Dirty Duckling](https://dssg.github.io/triage/dirtyduck/dirty_duckling/) de DSSG triage —
la forma rápida de tantear el terreno antes del
[caso de estudio DirtyDuck](/triage-pg/es/tutorials/dirtyduck/) completo.

Necesitas: **Docker**, **[uv](https://docs.astral.sh/uv/)** y un checkout de
[ccd-ia/triage-pg](https://github.com/ccd-ia/triage-pg). Todo se ejecuta desde la raíz
del repo. Unos diez minutos en total; la base de datos de inspecciones de comida y el
experimento corren completamente en tu máquina.

## Paso 1 — la CLI existe

```bash
uv sync --extra dev --extra dashboard
uv run triage --version
```

**PASS:** se imprime la versión:

```text
triage-pg 1.0.0
```

**Si falla:** `uv: command not found` → instala uv
(`curl -LsSf https://astral.sh/uv/install.sh | sh`). Un error de resolución de Python
→ necesitas Python 3.12+ (`uv python install 3.12`).

## Paso 2 — la base de datos del tutorial está arriba

```bash
just tutorial-up          # docker compose: builds + starts the food DB
pg_isready -h 127.0.0.1 -p 5440
```

**PASS:**

```text
127.0.0.1:5440 - accepting connections
```

**Si falla:** Docker no está corriendo (inicia Docker Desktop / `dockerd`), o el
puerto 5440 está ocupado — asigna otro puerto y vuelve a ejecutar:
`export DIRTYDUCK_PG_PORT=5444 && just tutorial-up` (luego usa ese puerto y ajusta
`dirtyduck-database.yaml` en consecuencia). La primera build tarda unos minutos;
`just tutorial-logs` muestra el progreso.

## Paso 3 — el esquema de resultados existe

```bash
uv run triage --dbfile dirtyduck-database.yaml db upgrade
```

**PASS:** las migraciones pasan en cascada y termina con:

```text
Database upgraded.
```

La base de datos de comida viene con las tablas *fuente* (`raw`, `clean`,
`ontology.*`); esto crea el esquema `triage` — experimentos, runs, el DAG de
artefactos, predicciones append-only, las funciones de evaluación dentro de
PostgreSQL — vía alembic, de forma idempotente (volver a ejecutarlo es un no-op).

## Paso 4 — la configuración valida

```bash
uv run triage --dbfile dirtyduck-database.yaml analyze-config example/dirtyduck/experiment.yaml
```

**PASS:** un reporte en panel **sin errores** — los splits temporales, un grid de
modelos de 5, y los resúmenes del SQL de la cohorte y de las etiquetas:

```text
  Avg train as_of dates     2.5
  Model grid size             5
╭──────────── Label Configuration ────────────╮
│ Label name: failed_inspections              │
│ SQL: select entity_id, bool_or(result =     │
│ 'fail')::integer as outcome from            │
│ ontology.events where {as_of_date}…         │
╰─────────────────────────────────────────────╯
```

Este es el mismo validador que el write-webapp ejecuta antes de aceptar un envío —
los errores regresan direccionados por ruta (`temporal_config.…`,
`label_config.query`) para que sepas exactamente qué corregir.

## Paso 5 — el pipeline corre de extremo a extremo

```bash
uv run triage --dbfile dirtyduck-database.yaml run \
  example/dirtyduck/experiment.yaml --project-path /tmp/dirtyduck-run
```

Un solo comando recorre todo el pipeline — unos minutos en una laptop:

![El pipeline: cohorte+etiquetas → features (DFS, joins as-of) → matrices → entrenamiento+predicción → evaluación dentro de la base de datos](../../../../assets/tutorials/pipeline-5box.svg)

Construye la cohorte y las etiquetas, genera features correctas
punto-en-el-tiempo (los joins as-of de featurizer), ensambla las matrices de
entrenamiento/prueba por cada split temporal, entrena un grid pequeño, agrega
predicciones, y evalúa dentro de la base de datos.

**PASS:** la terminal termina con exactamente esta forma:

```text
Experiment b9e38fd8f366… completed: 1 run(s), 20 model(s), 268860 prediction(s),
120 evaluation(s).
  run <your-run-id>… (all-features): 20 model(s), 268860 prediction(s), 120
evaluation(s).
storage: /tmp/dirtyduck-run
```

Dos cosas que revisar más allá de los conteos:

- **Tu hash de experimento (experiment_hash) también debe ser `b9e38fd8f366…`.** El
  hash se calcula a partir del *problema* (cohorte + label + configuración temporal,
  nada más) — si el tuyo difiere, editaste la configuración; ese es un experimento
  distinto, que es exactamente el contrato de reproducibilidad funcionando.
- El run id que aparece después es solo tuyo — cada intento obtiene uno nuevo.

**Si falla** a mitad del run, el error nombra la etapa que falló (cohorte, etiquetas,
features, matriz, modelo). Volver a ejecutar es seguro: los artefactos completados
están content-addressed y hacen cache-hit, así que un re-run reanuda en lugar de
rehacer.

## Paso 6 — los resultados son consultables

```bash
uv run triage --dbfile dirtyduck-database.yaml leaderboard b9e38fd8
```

**PASS:** una tabla ordenada por ranking — 5 grupos de modelos × 4 splits de prueba
(fechas as-of (as_of_date) 2015-07 → 2017-01), `auc_roc` por defecto, con regresiones
logísticas y ensambles de árboles intercambiando lugares en la cima:

```text
  Group   Model   Algorithm              Metric    As-of        Value
  5       20      ScaledLogisticRegre…   auc_roc   2017-01-01   0.5751
  4       19      ScaledLogisticRegre…   auc_roc   2017-01-01   0.5748
  3       18      RandomForestClassif…   auc_roc   2017-01-01   0.5612
  …
```

Los prefijos de hash funcionan en todos los lugares donde la CLI acepta un hash, al
estilo git.

## PASS — ahora el recorrido de cinco minutos

Tu instalación funciona. Apunta el dashboard a la misma base de datos y mira lo que
acabas de construir:

```bash
just serve 8001    # then open http://127.0.0.1:8001
```

(El dashboard usa la misma resolución de `PG*`/dbfile que la CLI; la ruta más rápida
es `cp dirtyduck-database.yaml database.yaml` antes de servir.)

![El resumen del experimento: grupos de modelos × splits temporales, con el mejor por split resaltado](../../../../assets/tutorials/experiment-overview.png)

Cinco cosas que valen 60 segundos cada una:

1. **El encabezado del experimento** — la píldora `classification` y los sparklines
   por split de cohorte / %-etiquetado / base-rate. El chip de hash es el mismo
   `b9e38fd8…` que imprimió la CLI.
2. **El heatmap** (pestaña Overview) — grupos de modelos × splits; la celda resaltada
   es el mejor modelo por split; haz clic en una para abrir su model card.
3. **Un model card** — curvas de umbral (precision/recall conforme barres el tamaño
   de lista k), histograma de scores, importancias de features.
4. **La pestaña Derivation** — el DAG de artefactos content-addressed que construyó el
   run; vuelve a ejecutar el mismo comando y observa cómo todo hace cache-hit.
5. **La pestaña Audition** — las reglas de selección de modelos de DSSG (distancia
   respecto al mejor, regret) calculadas en PostgreSQL.

## Qué sigue

- El [**caso de estudio DirtyDuck**](/triage-pg/es/tutorials/dirtyduck/) completo —
  los mismos datos, toda la discusión: alerta temprana vs priorización de recursos,
  fuga (leakage), fairness, selección de modelos.
- El [resumen en una página](https://ccd-ia.github.io/triage-pg/onboarding.html)
  para la vista del sistema de un vistazo.
- `just tutorial-down` detiene la base de datos; `just tutorial-clean` la elimina por
  completo (contenedores, imágenes, volúmenes).
