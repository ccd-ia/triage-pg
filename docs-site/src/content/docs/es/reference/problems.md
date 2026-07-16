---
title: El espacio de problemas — dos ejes ortogonales
description: Los cuatro tipos de problema (lo que predice el modelo) y los tres regímenes de observación (quién recibe una etiqueta y por qué) — de forma exhaustiva, un eje a la vez.
sidebar:
  order: 0
  label: El espacio de problemas
---

Cada experimento de triage-pg se ubica en **dos ejes ortogonales**, declarados
por dos claves de configuración:

- **`problem_type`** — *lo que predice el modelo y cómo se puntúa*:
  `classification` · `regression_ranking` · `regression` · `survival`.
  Este eje es parte de la identidad del experimento (cambiarlo es un problema
  nuevo) e impulsa la maquinaria: las columnas de etiqueta (label), la familia
  de estimadores, las funciones de evaluación.
- **`task_framing`** — *el régimen de observación*: quién recibe una etiqueta
  y por qué — `early_warning` · `resource_prioritization` · `visit_level`.
  Este eje es metadato neutral respecto a la identidad: cambia cómo debes
  *leer* los números, no cómo se calculan.

Los ejes se componen libremente; esta página enseña deliberadamente cada eje
**una sola vez** en lugar de enumerar las combinaciones — al final verás por
qué no hace falta ninguna matriz.

Contexto compartido para todo lo que sigue: sea cual sea el tipo de problema,
triage-pg ejecuta la misma columna vertebral **score → rank → evaluate**
(ADR-0010). El modelo emite un puntaje (score) por entidad; las entidades se
ordenan según él; la evaluación lee las predicciones ordenadas y de solo
anexado (append-only) en la base de datos. Los tipos de problema difieren en
lo que *significa* el puntaje y en qué funciones de evaluación aplican.

---

## Eje 1 — los cuatro tipos de problema

### `classification`

**Planteamiento de la pregunta.** «¿Le ocurrirá X a esta entidad dentro de la
ventana de la etiqueta?» — *¿esta instalación reprobará una inspección en 6
meses? ¿esta solicitud tomará más de 14 días?* La pregunta de sí/no del equipo
de política pública, hecha en un momento en el tiempo.

**Forma de la etiqueta.** Una fila por entidad con un `outcome` binario (0/1).
De la configuración de DirtyDuck:

```sql
select entity_id,
       bool_or(result = 'fail')::integer as outcome
from ontology.events
where {as_of_date}::date <= date
  and date < {as_of_date}::date + {label_timespan}
group by entity_id
```

**Lo que produce el modelo.** Un puntaje en [0, 1] — la probabilidad de clase
positiva del estimador — usado como clave de ranking. El puntaje *no* es una
decisión; el corte top-k sobre el que actúas se elige en tiempo de
evaluación/despliegue, no queda fijado en el entrenamiento.

**Evaluación.** Valores por defecto: `precision@` y `recall@` en los cortes
`100_abs` y `10_pct`, `auc_roc`, `average_precision` — todos en PL/pgSQL sobre
la tabla de predicciones. Precision@k es la métrica operativa («si actuamos
sobre los top k, ¿con qué frecuencia acertamos?»); el AUC resume la calidad
del ordenamiento con independencia de cualquier corte.

**Estimadores.** Cualquier clasificador de sklearn por ruta de clase
(`sklearn.tree.DecisionTreeClassifier`,
`sklearn.ensemble.RandomForestClassifier`, …) más el
`ScaledLogisticRegression` de triage (escalado min-max + LR, para que los
coeficientes sean comparables y se persistan como β con signo / razones de
momios).

**Características — cuándo elegirlo.** El valor por defecto para el triage de
política pública: los resultados (outcomes) son naturalmente binarios
(reprueba/no reprueba, lento/rápido, financiado/no financiado), el entregable
es una lista ordenada con un corte de capacidad, y las partes interesadas
razonan en términos de precision/recall.

**Errores comunes.** El desbalance de clases vuelve inútil a la exactitud
(accuracy) — lee siempre las métricas contra la tasa base. No pongas el umbral
del puntaje en 0.5 «porque es una probabilidad»: el corte es una decisión de
*capacidad*. Y una etiqueta binaria desecha la magnitud — si importa el
«cuánto/cuánto tiempo», mira los otros tres tipos.

**Ejemplo resuelto.**

```yaml
problem_type: classification
label_config:
  name: failed_inspections
  query: |
    select entity_id, bool_or(result = 'fail')::integer as outcome
    from ontology.events
    where {as_of_date}::date <= date
      and date < {as_of_date}::date + {label_timespan}
    group by entity_id
```

Configuración completa versionada:
[`example/dirtyduck/experiment.yaml`](https://github.com/ccd-ia/triage-pg/blob/main/example/dirtyduck/experiment.yaml)
— ejecútala de principio a fin en el [tutorial de DirtyDuck](/triage-pg/es/tutorials/dirtyduck/).

### `regression_ranking`

**Planteamiento de la pregunta.** «¿*Cuánto* de X acumulará esta entidad — y
quién acumula más?» — *¿cuántas violaciones amontonará esta instalación?* El
objetivo es continuo, pero el entregable sigue siendo una lista ordenada: te
importa *quién es el peor*, más que el número exacto.

**Forma de la etiqueta.** Una fila por entidad con un `outcome` continuo. De la
configuración de regresión versionada:

```sql
select entity_id,
       sum(coalesce(jsonb_array_length(violations), 0))::double precision as outcome
from ontology.events
where {as_of_date}::date <= date
  and date < {as_of_date}::date + {label_timespan}
group by entity_id
```

**Lo que produce el modelo.** La magnitud predicha, usada directamente como
clave de ranking — la cima de la lista es «el mayor número de violaciones
predichas».

**Evaluación.** Los valores por defecto de la familia de regresión: `rmse`,
`mae`, `r2` — seleccionables por configuración mediante el bloque `evaluation:`
(el ejemplo versionado selecciona solo `rmse` + `mae`, para mostrar la
anulación). Las columnas de rank se siguen poblando, así que las listas top-k y
las vistas ordenadas del dashboard funcionan exactamente igual que en
classification.

**Estimadores.** Regresores de sklearn por ruta de clase
(`sklearn.ensemble.RandomForestRegressor`, modelos lineales, …).

**Características — cuándo elegirlo.** El ADR-0010 lo convierte en la **vía
principal para objetivos continuos**: conserva el entregable de lista ordenada
sobre el que actúan los equipos de política pública, mientras entrena con la
señal continua más rica en lugar de una versión binarizada de ella.

**Errores comunes.** El RMSE queda dominado por la cola en objetivos de conteo
sesgados — léelo junto al MAE. No colapses la etiqueta continua a 0/1 «para
volverla classification»; si te descubres eligiendo un umbral para binarizar,
casi siempre estás mejor aquí. Los empates de ranking en cero (muchas entidades
sin eventos) son reales — el ranking interesante vive en la cola.

**Ejemplo resuelto.**

```yaml
problem_type: regression_ranking
label_config:
  name: violation_count
  query: |
    select entity_id,
           sum(coalesce(jsonb_array_length(violations), 0))::double precision as outcome
    from ontology.events
    where {as_of_date}::date <= date
      and date < {as_of_date}::date + {label_timespan}
    group by entity_id
evaluation:
  regression_metrics: [rmse, mae]   # override; default adds r2
```

Configuración completa versionada:
[`example/dirtyduck/experiment-regression.yaml`](https://github.com/ccd-ia/triage-pg/blob/main/example/dirtyduck/experiment-regression.yaml).

### `regression`

**Planteamiento de la pregunta.** «¿Cuál *será* el valor de X?» — predicción
puntual pura, donde el número mismo es el entregable: un costo pronosticado,
una carga de casos, una duración usada aguas abajo en operaciones aritméticas.

**Forma de la etiqueta.** Idéntica a `regression_ranking` — un `outcome`
continuo (la configuración de regresión pura versionada reutiliza la etiqueta
de conteo de violaciones tal cual). Los dos tipos difieren en *la intención y
el énfasis de la evaluación*, no en el esquema de la etiqueta: declarar
`regression` dice «la magnitud es el producto», declarar `regression_ranking`
dice «el ordenamiento es el producto».

**Lo que produce el modelo.** El valor predicho. Las columnas de ranking se
siguen calculando (la columna vertebral es compartida), pero nada aguas abajo
asume un corte de capacidad.

**Evaluación.** `rmse`, `mae`, `r2` (el valor por defecto de la familia;
seleccionable por configuración — el ejemplo versionado solicita los tres
explícitamente).

**Estimadores.** La misma familia de regresores de sklearn que
`regression_ranking`.

**Características — cuándo elegirlo.** Cuando un consumidor necesita el número:
presupuestación, aritmética de dotación de personal, alimentar otro modelo. Si
algún humano leerá la salida como «¿quién primero?», prefiere
`regression_ranking` — la misma etiqueta, un encuadre más honesto.

**Errores comunes.** El R² sobre splits temporales puede engañar (la varianza
del objetivo cambia con el tiempo — compara dentro de un split, no entre
splits). Un buen RMSE puede coexistir con un ranking inútil y viceversa;
declara el tipo según lo que realmente vas a consumir.

**Ejemplo resuelto.**

```yaml
problem_type: regression
label_config:
  name: violation_count
  query: |            # same continuous label as regression_ranking
    select entity_id,
           sum(coalesce(jsonb_array_length(violations), 0))::double precision as outcome
    from ontology.events
    where {as_of_date}::date <= date
      and date < {as_of_date}::date + {label_timespan}
    group by entity_id
evaluation:
  regression_metrics: [rmse, mae, r2]
```

Configuración completa versionada:
[`example/dirtyduck/experiment-pure-regression.yaml`](https://github.com/ccd-ia/triage-pg/blob/main/example/dirtyduck/experiment-pure-regression.yaml).

### `survival`

**Planteamiento de la pregunta.** «¿*Cuánto tiempo* falta hasta que ocurra X —
sabiendo que para algunas entidades dejaremos de observar antes de que
ocurra?» — *tiempo hasta la resolución, tiempo hasta la falla, tiempo hasta el
reingreso.* La pregunta que classification desecha por partida doble: la
magnitud *y* la diferencia entre «no ocurrió» y «aún no había ocurrido cuando
dejamos de mirar».

**Forma de la etiqueta.** Dos columnas por entidad — `(duration,
event_observed)` (el esquema de etiqueta listo para supervivencia del
ADR-0010). `event_observed = false` es **censura**: la ventana se cerró
primero, así que `duration` es una *cota inferior*, no una ausencia del
evento. De la configuración de supervivencia de Chicago 311:

```sql
select
  e.entity_id,
  case
    when e.closed_date is not null
     and e.closed_date < {as_of_date}::date + {label_timespan}
    then extract(epoch from (e.closed_date - e.created_date)) / 86400.0
    else extract(epoch from (({as_of_date}::date + {label_timespan}) - e.created_date)) / 86400.0
  end as duration,
  (e.closed_date is not null
   and e.closed_date < {as_of_date}::date + {label_timespan}) as event_observed
from ontology.entities as e
where e.created_date >= {as_of_date}::date - interval '1 month'
  and e.created_date <  {as_of_date}::date
```

**Lo que produce el modelo.** Un puntaje de riesgo — más alto significa que el
evento se espera *antes*. Se ordena como cualquier otro puntaje; la semántica
es de riesgo relativo (hazard), no de una probabilidad ni de una duración.

**Evaluación.** El **índice de concordancia** (`c_index`) — de todos los pares
comparables, ¿con qué frecuencia la entidad de mayor riesgo experimenta el
evento primero? — calculado por una función PL/pgSQL que coincide con el
`concordance_index_censored` de scikit-survival hasta 1e-9 (ADR-0026). Las
filas censuradas participan exactamente en la medida en que son comparables,
que es justamente el punto.

**Estimadores.** scikit-survival tras el extra `survival` (`uv sync --extra
survival`) — el wrapper versionado es `ScaledCoxPHSurvivalAnalysis` (riesgos
proporcionales de Cox escalados).

**Características — cuándo elegirlo.** Siempre que «cuánto tiempo» sea la
verdadera pregunta y la censura sea real — tickets abiertos, casos en curso,
suscripciones. El dashboard se adapta: el panel de la entidad muestra
`duration` + evento/censurado por cada fila de etiqueta, y la tarjeta de tasa
base del encabezado se convierte en una **tasa de eventos** (proporción de
etiquetas cuyo evento fue observado).

**Errores comunes.** Tratar las filas censuradas como «sin evento = 0»
convierte silenciosamente el problema en una classification sesgada — el error
de supervivencia más común de todos. Las unidades de duración son lo que emita
tu SQL (días, en este caso) — sé consistente. Un C-index necesita pares
comparables: una ventana tan corta que casi nada se observe lo deja indefinido.

**Ejemplo resuelto.**

```yaml
problem_type: survival        # requires: uv sync --extra survival
label_config:
  name: time_to_resolution
  query: |                    # emits (duration, event_observed) — see above
    …
grid_config:
  'triage.component.catwalk.estimators.survival.ScaledCoxPHSurvivalAnalysis':
    alpha: [0.1]
```

Configuraciones completas versionadas:
[`example/chicago311/experiment-survival.yaml`](https://github.com/ccd-ia/triage-pg/blob/main/example/chicago311/experiment-survival.yaml)
y
[`example/dirtyduck/experiment-survival.yaml`](https://github.com/ccd-ia/triage-pg/blob/main/example/dirtyduck/experiment-survival.yaml)
— ejecútalas en vivo en el [tutorial de Chicago 311](/triage-pg/es/tutorials/chicago311/).

---

## Eje 2 — los tres regímenes de observación

`problem_type` dice qué predice el modelo. **`task_framing`** dice algo que las
matemáticas no pueden: *¿bajo qué condiciones la realidad te entrega una
etiqueta?* Es una clave de configuración opcional y neutral respecto a la
identidad (agregarla o cambiarla nunca bifurca el hash de un experimento) que
el dashboard convierte en una insignia junto al tipo de problema y en contexto
sobre la tarjeta de %-etiquetado.

### `early_warning`

**Semántica de observación.** El outcome se **registra administrativamente para
cada miembro de la cohorte** — un registro, un libro mayor, un sistema de
registro cierra cada caso. Nadie tiene que actuar para que la verdad exista.

**Expectativa de %-etiquetado.** ~**100%** una vez que la ventana madura. El
dashboard trata cualquier valor menor como una señal de alerta («las etiquetas
deberían cubrir la cohorte») — en este régimen, las etiquetas faltantes
significan una consulta de etiqueta rota o datos rotos, no un hecho de la vida.

**Qué significa la tasa base.** La prevalencia *poblacional*: «el X% de todas
las solicitudes son lentas», «el Y% de todos los proyectos quedan sin
financiamiento». Es el número que puedes citar públicamente.

**Implicaciones de sesgo de selección.** Mínimas del lado de la etiqueta — el
modelo aprende de todos. (Tu definición de cohorte todavía puede seleccionar;
la etiqueta no.)

**Cómo actúas sobre la lista.** Marcar, escalar, priorizar la atención — la
entidad tendría su outcome de todos modos; estás eligiendo dónde *mirar
temprano*, e incluso ignorar la lista no te cuesta nada en cobertura futura de
etiquetas.

**Configuración + ejemplo.**

```yaml
task_framing: early_warning
# the signature move: absence of the event is a real 0, knowable for everyone
label_config:
  query: |
    select e.entity_id,
           coalesce(bool_or(ev.result = 'fail'), false)::integer as outcome
    from ontology.entities e
    left join ontology.events ev on …   -- LEFT JOIN + coalesce = full coverage
    where …cohort condition…
    group by e.entity_id
```

Ejemplos versionados: el `slow_resolution` de Chicago 311 (la resolución se
registra para cada solicitud) y el gemelo EIS de DirtyDuck
([`experiment-eis.yaml`](https://github.com/ccd-ia/triage-pg/blob/main/example/dirtyduck/experiment-eis.yaml)).

### `resource_prioritization`

**Semántica de observación.** El outcome existe **solo para las entidades sobre
las que alguien actuó** — inspeccionadas, auditadas, visitadas. Para el resto,
la verdad nunca se generó: su etiqueta es NULL, que significa *desconocido*, no
«no».

**Expectativa de %-etiquetado.** **Bastante por debajo del 100%** — la tasa de
acción. La configuración base de DirtyDuck se sitúa en ~54%. El dashboard
muestra «etiquetas selectivas — se espera <100%» en lugar de alarmarse.

**Qué significa la tasa base.** Una tasa **condicional**: «entre las
instalaciones *inspeccionadas*, el 28% reprueba». No es la tasa poblacional y
jamás debe citarse como tal — las configuraciones gemelas de DirtyDuck sitúan
los mismos datos en 0.277 condicional vs. 0.116 poblacional.

**Implicaciones de sesgo de selección.** La grande. El modelo se entrena con
entidades *seleccionadas por el proceso histórico* (quejas, calendarios, juicio
humano), así que aprende «entre el tipo de lugares que se inspeccionan…».
Desplegarlo cambia quién se inspecciona, lo que cambia las etiquetas futuras —
el bucle de retroalimentación es intrínseco al régimen, y pretender que el
modelo habla sobre toda la población es el fracaso clásico.

**Cómo actúas sobre la lista.** La lista *es* la acción: decide quién recibe el
recurso escaso. La auditoría de equidad importa más aquí (la intervención suele
ser una carga — la rama «punitiva» del árbol de equidad), y las etiquetas del
siguiente periodo vendrán de quienes hayas elegido.

**Configuración + ejemplo.**

```yaml
task_framing: resource_prioritization
# the signature move: labels come FROM the action stream; no row = unknown
label_config:
  query: |
    select entity_id, bool_or(result = 'fail')::integer as outcome
    from ontology.events          -- only acted-on entities appear here
    where …window…
    group by entity_id
```

Ejemplo versionado: el
[`experiment.yaml`](https://github.com/ccd-ia/triage-pg/blob/main/example/dirtyduck/experiment.yaml)
base de DirtyDuck — el [tutorial de DirtyDuck](/triage-pg/es/tutorials/dirtyduck/)
construye su lección central sobre el contraste con el gemelo EIS.

### `visit_level`

**Semántica de observación.** La etiqueta se adhiere a un **evento, no a un
par entidad-periodo**: cada visita/interacción/transacción obtiene su propio
outcome («¿*esta* visita terminó en una violación? ¿*esta* llamada resolvió el
problema?»). Una entidad puede aportar muchas filas etiquetadas por ventana —
o ninguna, si no tuvo eventos.

**Expectativa de %-etiquetado.** 100% *de los eventos* — cada visita que
ocurrió tiene un outcome — pero la cobertura está guiada por eventos: el conteo
de filas de la cohorte sigue la actividad, no el universo de entidades.

**Qué significa la tasa base.** Una tasa **por evento**: «el X% de las visitas
terminan en una violación». Citarla como un riesgo a nivel de entidad confunde
a las entidades ocupadas con las riesgosas.

**Implicaciones de sesgo de selección.** Heredadas de lo que sea que genere los
eventos: si las visitas se programan según el riesgo, el flujo de eventos en sí
está seleccionado. El régimen es honesto respecto a la *unidad* (la visita)
pero no automáticamente respecto a *cuáles* visitas existen.

**Cómo actúas sobre la lista.** Enrutamiento y preparación por evento: cuáles
visitas próximas necesitan al inspector senior, cuáles llamadas entrantes van a
la cola del especialista — decisiones sobre *ocasiones*, no designaciones
permanentes de entidad.

**Configuración + ejemplo.** El `entity_id` de la cohorte es el *evento* (la
visita), no el actor de larga vida detrás de él — de la variante versionada de
DirtyDuck («¿*esta* inspección encontrará una violación?»):

```yaml
task_framing: visit_level
# the signature move: the cohort row IS the event
cohort_config:
  name: upcoming_visits
  query: |
    select ev.event_id as entity_id
    from ontology.events as ev
    where {as_of_date}::date <= ev.date
      and ev.date < {as_of_date}::date + interval '1 month'
label_config:
  name: visit_finds_violation
  query: |
    select ev.event_id as entity_id,
           (ev.result = 'fail')::integer as outcome
    from ontology.events as ev
    where {as_of_date}::date <= ev.date
      and ev.date < {as_of_date}::date + {label_timespan}
```

Configuración completa versionada:
[`example/dirtyduck/experiment-visits.yaml`](https://github.com/ccd-ia/triage-pg/blob/main/example/dirtyduck/experiment-visits.yaml)
— nota sus dos advertencias de honestidad, expresadas en el archivo: las
visitas históricas sustituyen a una tabla de programación (la aproximación
estándar a nivel de visita), y el `type` de la visita deliberadamente *no* es
una característica (feature) (la existencia de una visita disparada por una
queja solo es conocible cuando llega la queja).

---

## Por qué no hay una matriz de 4×3

Los ejes responden preguntas distintas — *¿qué significa el puntaje?* frente a
*¿bajo qué condiciones la realidad etiqueta los datos?* — y se componen sin
términos de interacción: una etiqueta de supervivencia puede estar
completamente observada (early-warning) o generarse solo por inspecciones
(resource-prioritization); una etiqueta de classification puede adherirse a
visitas. Enseñar doce combinaciones repetiría las mismas dos lecciones doce
veces. La prueba viviente de que los ejes son independientes es el propio
DirtyDuck: **tres configuraciones versionadas sobre un mismo conjunto de datos,
una por régimen, todas `classification`** — la base (inspecciones), el gemelo
EIS (alerta temprana) y la variante de visitas (a nivel de visita) difieren
únicamente en el SQL de cohorte/etiqueta y en la marca de encuadre, mientras
que la maquinaria del modelo nunca se entera.

Dónde ver cada eje ejercitado de verdad: los
[tutoriales](/triage-pg/es/tutorials/) (los cuatro tipos de problema, ambos
regímenes a nivel de entidad) y la escueta referencia dentro del repo
[`docs/problem-types.md`](https://github.com/ccd-ia/triage-pg/blob/main/docs/problem-types.md).
