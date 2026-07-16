---
title: La línea de comandos, flujo de trabajo por flujo de trabajo
description: El CLI de triage es el producto completo — cada superficie, con salida real.
sidebar:
  order: 3
  label: Recorrido por el CLI
---

El CLI no es un acompañante del dashboard — es el **producto completo**
(ADR-0012: núcleo completo sin interfaz gráfica, headless). Todo lo que sigue
lee las mismas vistas SQL que renderiza el dashboard. Toda la salida mostrada
es real, capturada contra las bases de datos de los tutoriales.

Dos comodidades ergonómicas presentes en todo el recorrido:

- **resolución de la conexión**: `--dbfile <yaml>` › `database.yaml` en el cwd ›
  `DATABASE_URL` › variables de entorno `PG*`. El log de arranque imprime la URL
  resuelta con la contraseña enmascarada.
- **prefijos de hash**: en cualquier lugar donde un comando tome un hash de
  experimento o de artefacto, funciona un prefijo único al estilo de git
  (`b9e38fd8` para `b9e38fd8f366…`).

## Comprobación rápida

```console
$ uv run triage --version
triage-pg 1.0.0
```

## Configurar la base de datos de un proyecto

```console
$ triage db upgrade          # alembic → the triage schema, idempotent
Database upgraded.
```

(`triage db history|stamp|downgrade` para el resto de la superficie de alembic;
`triage project create|drop|list` para los ciclos de vida de
una-base-de-datos-por-proyecto gestionados por el registry.)

## Validar antes de ejecutar

```console
$ triage analyze-config example/dirtyduck/experiment.yaml
  Avg train as_of dates     2.5
  Model grid size             5
╭──────────── Label Configuration ────────────╮
│ Label name: failed_inspections              │
│ SQL: select entity_id, bool_or(result =     │
│ 'fail')::integer as outcome …               │
╰─────────────────────────────────────────────╯
```

El mismo validador respalda el formulario de envío de la webapp — los errores
regresan direccionados por ruta (`temporal_config.…`, `label_config.query`).

## Ejecutar

```console
$ triage run example/dirtyduck/experiment.yaml --project-path /tmp/dirtyduck-run
…
Experiment b9e38fd8f366… completed: 1 run(s), 20 model(s), 268860 prediction(s),
120 evaluation(s).
storage: /tmp/dirtyduck-run
```

Volver a ejecutar siempre es seguro: los artefactos son direccionados por
contenido, así que las etapas sin cambios dan cache-hit y el run se reanuda
donde los insumos realmente cambiaron.

## Leer resultados

```console
$ triage leaderboard b9e38fd8
  Group   Model   Algorithm              Metric    As-of        Value
  5       20      ScaledLogisticRegre…   auc_roc   2017-01-01   0.5751
  4       19      ScaledLogisticRegre…   auc_roc   2017-01-01   0.5748
  …

$ triage models b9e38fd8
  Group   Algorithm            Models   Avg ± σ           Max regret   Avg fit
  5       ScaledLogisticReg…   4        0.5850 ± 0.0279   0.0118       0.8s
  4       ScaledLogisticReg…   4        0.5823 ± 0.0162   0.0207       0.1s
  …
```

`triage models <hash> --group N` profundiza en los miembros de un grupo;
`triage model show <id>` imprime la tarjeta de un modelo con deciles de
calibración.

## Seleccionar un modelo

```console
$ triage audition b9e38fd8
  Group   Splits   Avg ± σ           Dist. from best (avg)   Max regret   Regret next time (max)
  5       4        0.5850 ± 0.0279   0.0032                  0.0118       0.0118
  4       4        0.5823 ± 0.0162   0.0060                  0.0207       0.0207
  …
```

Las reglas de selección de DSSG sobre las vistas de audition en PostgreSQL:
elige por estabilidad a lo largo de los splits, no por una celda afortunada.
`--json` en los comandos de lectura emite salida legible por máquina para
scripting.

## Diagnosticar

```console
$ triage postmodel crosstabs 20 -p 100_abs
441 crosstab row(s) persisted.
  As-of        Feature                            Selected   Rest     Ratio
  2017-01-01   facilities.zip_code=60622          0.6800     0.0280   24.32
  2017-01-01   facilities.facility_type=mobile…   0.0300     0.0056   5.38
  …
```

Los crosstabs responden «¿qué caracteriza al top-k?»; `triage postmodel
error-tree <id>` ajusta un árbol interpretable y superficial sobre los errores
del modelo («¿dónde falla?»); `triage postmodel compare <a> <b>` calcula el
traslape de listas. Se calcula una vez a partir de la matriz, se persiste en
PostgreSQL, se puede leer en todas partes (ADR-0011).

## Operar

```console
$ triage score 20 2019-12-01
Forward-scored model 20 at 2019-12-01 (append-only).
```

El punto de entrada de monitoreo (ADR-0027) — prográmalo con cron o
EventBridge; cada invocación anexa predicciones estampadas con `scored_at` y
las vistas de monitoreo (drift, volumen, outcomes realizados) se acumulan.
Superficies de contabilidad: `triage source list` (fijaciones de versión),
`triage archive <hash>` (archivar suavemente un experimento), `triage gc`
(recolectar artefactos inalcanzables desde cualquier raíz), `triage runs
status` (backfill de AWS Batch en el perfil cloud).

## A dónde seguir

- [Arquitectura](/triage-pg/es/reference/architecture/) — las tablas que estos
  comandos leen y escriben.
- [El recorrido por el dashboard](/triage-pg/es/reference/dashboard/) — las
  mismas superficies, renderizadas.
- La [prueba de humo Dirty Duckling](/triage-pg/es/tutorials/dirtyduckling/)
  para ejecutar esto de principio a fin por tu cuenta.
