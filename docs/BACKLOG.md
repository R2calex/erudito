# Erudito v3 — Backlog

## Critical (next session)

- [ ] **Registry thread safety**: Replace all 13 direct `registry._data["projects"]` mutations in main.py with `registry.update_fields()`. The method exists but isn't used yet. Concurrent scan/NLM tasks can corrupt state. (review #2)
- [x] **Run tier migration**: ~~Execute `python scripts/migrate-tiers.py`~~ — Done. Tiers assigned, Qdrant migrated, legacy cleanup complete.
- [x] **Legacy cleanup**: ~~agent_knowledge collection, orphan points, dead code~~ — Done (commit 0a6cf6d).

## Manual Review Backlog (weekend)

Projects and items that could not be fully categorized/distilled automatically.
Audit date: 2026-03-26.

### Projects never curated (no knowledge extracted)

| Project | Docs | Issue | Action needed |
|---------|------|-------|---------------|
| `claude-contracts` | 130 | `curation_status=uncurated`, tier 3. Never curated despite having 130 docs. Likely too many files for auto-curation or path mismatch. | Review docs, curate manually or adjust scanner config |
| `node-reporter` | 0 | `status=pending`, 0 docs. Registered but never scanned — path may not exist or be empty. | Verify path exists, remove from registry if abandoned |

### Projects with only `direct` distillation (minimal knowledge)

These were distilled by raw doc embedding, not LLM or NLM. Quality is low — single generic note per project.

| Project | Notes | Source | Issue |
|---------|-------|--------|-------|
| `agent-eval` | 1 | direct | Tier 3, only 1 generic note. 3 docs available but not LLM-distilled. |
| `devops-agent` | 1 | direct | Tier 3, only 1 note ("agent-bootstrap-protocol"). 3 docs available. |
| `marker-mcp` | 1 | direct | Tier 3, only 1 note. 3 docs available. |
| `mesh-monitor` | 2 | direct | Tier 3, 2 generic notes. 6 docs available. |
| `openclaw-kubo` | 2 | direct | Tier 3, 2 notes. 2 docs available. |
| `qdrant-mcp` | 1 | direct | Tier 3, 1 note. 1 doc available. |
| `sariatu-docs` | 1 | direct | Tier 3, 1 note. 1 doc available. |

### Tier 1 projects blocked on NLM

These are high-priority projects that should have NLM-quality distillation but NLM circuit breaker is tripped.

| Project | NLM failures | Has LLM fallback? | Notes |
|---------|-------------|-------------------|-------|
| `erudito` | 30 | Yes (5 LLM notes) | NLM never succeeded. LLM fallback working. |
| `infra-mcp` | 29 | Yes (5 LLM notes) | NLM never succeeded. LLM fallback working. |
| `jasper` | 0 | Yes (5 LLM + 9 NLM) | Only project with NLM notes. Circuit OK. |
| `jasper-profiles` | 28 | Yes (5 LLM notes) | NLM never succeeded. LLM fallback working. |

### Recurring LLM distillation errors

Jasper had ~20+ consecutive `distill_llm_error` entries (2026-03-25) with `zen/minimax-m2.5` returning `Cannot read properties of undefined (reading 'prompt_tokens')`. This was fixed by the LiteLLM model name correction but may recur if the upstream model has issues.

### Summary — what to review this weekend

1. **`claude-contracts`**: 130 uncurated docs — biggest gap. Decide if worth curating or splitting.
2. **`node-reporter`**: Ghost project — verify or remove.
3. **7 tier-3 projects with `direct` notes**: Run `POST /curate/{project}` then `POST /scan/{project}` to trigger LLM distillation for each.
4. **NLM circuit breaker**: 14/16 projects have 28-30 NLM failures. Investigate NLM connectivity or accept LLM-only distillation as sufficient.

## Important (this week)

- [ ] **API authentication**: Add API key header check for write endpoints (/registry, /ingest, /classify, /nlm/reset, /scan, /curate). Tailscale provides network-level auth but process-level auth is missing. (review #3)
- [x] **Deprecation fix**: ~~Replace `asyncio.get_event_loop()` with `asyncio.get_running_loop()` in `_fallback_index_curated`~~ — Moot: `_fallback_index_curated` removed in legacy cleanup.
- [ ] **NLM session lock**: Add `asyncio.Lock` to protect `_session_id` / `_session_initialized` in notebooklm.py. (review #5)
- [ ] **Content hash**: Use full sha256 instead of truncated MD5 in `_content_hash`. (review #6)
- [ ] **Dynamic import cleanup**: Replace `__import__('core.registry')` in /ingest with proper import. (review #7)
- [ ] **sync_sources total count**: Fix double-counting of updated sources in total. (review #8)
- [ ] **Inbox path env var**: Make classify inbox path configurable via `CLASSIFY_INBOX` env var. (review #9)
- [x] **Curator section titles**: ~~Change Spanish titles to English~~ — Done in knowledge tiers implementation.
- [ ] **Deploy devops-agents on Kubo/Sariatu**: Endpoints deployed but projects need proper registration with remote paths. See memory: project_remote_nodes_pending.md

## Auto-Memory Integration (depends on Phase 0)

> **Goal:** Indexar Claude Code auto-memory (`~/.claude/projects/-home-r0calex-ai-lab/memory/`) como `knowledge_base` en Erudito para que la búsqueda agentica pueda recuperar decisiones, feedback y contexto histórico del mesh. Sinergia con AutoDream (consolidador de Anthropic, ya activo en `settings.json`): AutoDream limpia → Erudito reindexa → agentic search disponible mesh-wide. Cubre ~70% del use-case que MemPalace promete sin meter ese repo (ver `memory/project_mempalace_evaluation.md`).
>
> **IMPORTANT:** Casi todo este plan depende del resultado de **Fase 0**. Es muy posible que Erudito ya maneje 2-3 de las 5 fricciones identificadas y que la Fase 1 termine siendo un PR mucho más chico de lo listado abajo. NO empezar Fase 1 hasta tener el reporte de Fase 0.
>
> **Plan analizado:** 2026-04-08 — sesión Claude Code (auto-memory `project_mempalace_evaluation.md` + auditoría real de Erudito vía Explore agent).
>
> **UPDATE 2026-04-08 (post Fase 0):** Phase 0 ejecutada — ver sección "Phase 0 — Resultados" más abajo. La fricción real **#5 quedó descartada**, **#4 quedó condicional**, y apareció una **#6 crítica** que el plan original no anticipaba (curator agrupa por prefijo de filename → 10 proyectos `project_*` colapsan en un solo curated file). El usuario ratificó la solución correcta: **bypass del curator Y del NLM para auto-memory, manteniendo el linking metadata-driven con proyectos existentes** (ver "Refined Plan post Phase 0" más abajo).

### Las 5 fricciones identificadas (a confirmar en Fase 0)

| # | Fricción | Síntoma esperado | Verificación en Fase 0 |
|---|---|---|---|
| 1 | `MEMORY.md` se indexaría como contenido (es un índice, no conocimiento) | Vector store contaminado con embeddings de la lista de pointers | Buscar puntos en Qdrant con `source` que contenga `MEMORY.md` |
| 2 | Delta `compute_fs_delta()` por mtime+size, no por contenido | AutoDream reescribe cada 24h sin cambio semántico → re-distill innecesario, quema budget LLM/NLM | Modificar mtime sin tocar contenido y ver si re-distila |
| 3 | `SCAN_INTERVAL` global 15min vs cambios cada 24h | ~96 ciclos/día desperdiciados | Inspeccionar logs `_distill_loop` post-registro |
| 4 | Tier auto-assign por count de `.md` (≥5 → T1) → 14 archivos van a NLM | NLM circuit breaker tripped (ya está según backlog actual), gasto innecesario | Ver `tier` asignado al proyecto post-curate |
| 5 | `enricher.py` puede pisar el `type` del frontmatter (auto-memory usa `type: user|feedback|project|reference`) | Pierde filtrabilidad por tipo de memoria en Qdrant | Inspeccionar payload de un punto post-distill |

### Cambios mínimos en Erudito (Fase 1, ~40 LOC efectivas, **sujeto a Fase 0**)

| Cambio | Archivo | LOC aprox | Fricción |
|---|---|---|---|
| `exclude_patterns: list[str]` por KB en registry schema + filtro en scanner | `core/scanner.py:21+`, registry schema | ~15 | #1 |
| Hash de contenido (sha256) opcional en `compute_fs_delta()`, flag `content_hash: bool` | `core/scanner.py:177-193` | ~10 | #2 |
| `scan_interval_seconds: int` opcional por KB, respetado en `_distill_loop` | `main.py:463-473`, registry schema | ~8 | #3 |
| `tier_override: "T1"\|"T2"\|"T3"` opcional por KB, respetado en `compute_tier()` | `core/distiller.py:39-60` | ~6 | #4 |
| `enricher` usa `setdefault` en vez de asignar `type` (preservar frontmatter del autor) | `core/enricher.py:20-33` | ~1 | #5 |

Total: ~40 LOC + tests + actualización de schema del registry. **PR pequeño si las 5 fricciones se confirman; podría ser de 1-2 cambios si Fase 0 descarta varias.**

### Fases del plan

- [ ] **Fase 0 — Smoke test (HOY, 30 min, sin código)**. Registrar `ai-lab-memory` tal cual vía `POST /registry` con `type=knowledge_base` y observar:
  1. ¿Cuántos puntos en `nlm_notes` con `project=ai-lab-memory` post-scan?
  2. ¿`MEMORY.md` aparece como source de algún chunk? (confirma/descarta #1)
  3. ¿Una query agentica como "¿por qué evitamos Trivy?" devuelve `feedback_avoid_trivy.md`?
  4. ¿El `type` del frontmatter sobrevive en el payload Qdrant? (confirma/descarta #5)
  5. ¿Qué tier asignó? (confirma/descarta #4)
  6. ¿Re-scan sin cambios reales lo re-distila? (confirma/descarta #2)

- [ ] **Fase 1 — Cambios mínimos en Erudito (1 PR)**. Solo lo que Fase 0 confirme como necesario. Tabla arriba.

- [ ] **Fase 2 — Re-registrar auto-memory con config correcta**:
  ```json
  {
    "name": "ai-lab-memory",
    "path": "/home/r0calex/.claude/projects/-home-r0calex-ai-lab/memory",
    "type": "knowledge_base",
    "exclude_patterns": ["MEMORY.md"],
    "content_hash": true,
    "scan_interval_seconds": 86400,
    "tier_override": "T2"
  }
  ```
  Validar con queries agenticas reales del mesh ("¿qué decisiones tomamos sobre OpenClaw en Kubo?", "¿qué proyectos están desplegados en abril?", etc.).

- [ ] **Fase 3 — Trigger event-driven post-AutoDream**. En vez de poll cada 24h, hook (o cron a la 1am como fallback si AutoDream no expone hook propio todavía) que llama `POST /scan/ai-lab-memory` después de que AutoDream consolida. Reusar endpoint de scan existente o crear `/scan/{kb_name}` específico.

- [ ] **Fase 4 — Loop completo + extensión a otros nodos**. Auto-memory de proyectos adicionales (`-home-r0calex-desarrollos-openclaw`, etc.) como knowledge_bases adicionales. Considerar centralizar indexación en Hanzo y exponer vía MCP/API para que Kubo y Sariatu consulten la memoria del mesh sin tener que sincronizar archivos.

- [ ] **Fase 5 — Documentación (regla AI-Lab §4, no negociable)**:
  - SPEC: `~/ai-lab/erudito/docs/spec-auto-memory-integration.md` — schema changes del registry, semántica de los nuevos campos
  - IR: `~/ai-lab/erudito/docs/IR-2026-04-XX-auto-memory-kb.md` — diffs reales de los cambios de Fase 1
  - SOP: `~/ai-lab/erudito/docs/SOP-registering-memory-kb.md` — cómo registrar auto-memory de futuros proyectos como KB

### Phase 0 — Resultados (ejecutado 2026-04-08)

**Setup ejecutado:**
1. Container `erudito-v3` (4d up, healthy) tiene mounts: `~/ai-lab/knowledge:/app/knowledge:ro` + `~/ai-lab/erudito/data:/app/data:rw`. El path `~/.claude/projects/...` NO está montado.
2. **Workaround para Fase 0:** snapshot via `cp -av` de los 14 .md + MEMORY.md + .consolidate-lock a `~/ai-lab/knowledge/ai-lab-memory/` (visible desde container). **Snapshot dejado activo** para reanudar próxima sesión sin re-setup.
3. `POST /registry` con `type=knowledge_base` → registrado ok (`docker-patterns` ya era precedente exitoso de KB-no-git).
4. `POST /scan/ai-lab-memory` + `POST /curate/ai-lab-memory` → `status: synced`, `curation_status: curated`, sin errores.

**Resultados de las 5 fricciones originales:**

| # | Fricción | Resultado | Evidencia |
|---|---|---|---|
| 1 | `MEMORY.md` indexado como contenido | ✅ **CONFIRMADA** | Existe `curated/ai-lab-memory/memory.md` con `sources: [MEMORY.md]`. |
| 2 | Delta por mtime+size, no contenido | ✅ **CONFIRMADA por código** | `scanner.py:190`: `f"{rel_path}:{stat.st_size}:{int(stat.st_mtime)}"`. |
| 3 | `SCAN_INTERVAL` global 15min | ✅ **CONFIRMADA por diseño** | Variable única global, no per-KB. |
| 4 | Tier auto T1 → quema NLM | ⚠️ **DESCARTADA condicionalmente** | `computed_tier: 2` porque post-merge `curated_files=4`. **Resucita si arreglamos #6 y separamos archivos**. |
| 5 | `enricher` pisa `type` del frontmatter | ❌ **DESCARTADA** | `enricher.py:56` ya usa `if "type" not in fm:` (equiv setdefault). **Cero cambios necesarios**. |

**Bonus findings:**
- `.consolidate-lock` ignorado automáticamente por filtro de extensiones ✅ — no requiere `exclude_patterns` para este archivo específico.
- Path expansion `os.path.expanduser` ✅ funciona perfecto.
- `/search?q=mempalace&project=ai-lab-memory` devolvió "no information" — **distillation NO HA CORRIDO todavía** (last_distill: null), está pendiente del próximo ciclo del `_distill_loop`. Quality real de retrieval queda pendiente para próxima sesión.

### La fricción #6 crítica (descubierta en Phase 0)

**Síntoma:** El curator agrupó los 15 archivos en solo **4 curated files**:

| Curated file | Sources | Notas |
|---|---|---|
| `feedback.md` | 3 (`feedback_avoid_trivy`, `feedback_baseline_quality`, `feedback_zen_no_free`) | OK semánticamente |
| `max.md` | 1 | OK |
| `memory.md` | 1 (`MEMORY.md`) | Confirma fricción #1 |
| **`project.md`** | **10 proyectos completamente independientes** (~20KB) | **🔥 CATÁSTROFE de granularidad** |

**Mecánica del bug:** `core/curator.py:68`:
```python
first_token = re.split(r"[-_]", name)[0]
first_token_groups.setdefault(first_token, []).append(filename)
```
Toma el primer token del filename como "feature". Para SPEC-auth/IR-auth/SOP-auth funciona bien. Para auto-memory donde `project_*` es un namespace (no un feature compartido), colapsa Aegis + env-masker + erudito_v3 + erudito_agentic + kubo_openclaw + mempalace + mesh_improvements + security_guidance + session_hooks + zen_keys_split en un solo blob.

**Por qué es bloqueador:** cuando el distiller LLM procese ese blob de 20KB con sus 5 preguntas canónicas (`DISTILL_QUESTIONS`), va a sintetizar respuestas que mezclan los 10 proyectos como si fueran uno solo. Pregunta "¿qué es Aegis?" → respuesta contaminada con menciones a env-masker, mempalace, etc. Vector retrieval mejora algo (chunks distintos), pero la calidad de distillation se degrada brutalmente. **Sin resolver #6, indexar auto-memory en Erudito tiene rendimiento neto negativo.**

### Refined Plan post Phase 0 — Camino "atomic memory"

**Decisión del usuario (2026-04-08):** crear un camino dedicado donde auto-memory:
1. **Bypass del curator** — cada `.md` se trata como unidad atómica, sin merging por prefijo
2. **Bypass del NLM** — directo a embedding LLM, sin pasar por NotebookLM (ya está roto + es inapropiado para 14 archivos pequeños)
3. **Linking metadata-driven con proyectos existentes** — `project_aegis.md` se etiqueta con `linked_project: aegis` en payload Qdrant, derivado del filename + validado contra el registry. Cuando una query consulta "tell me about Aegis", recupera **tanto** los curated docs del repo `aegis` **como** los memory entries con `linked_project: aegis`. Cierra el loop entre conocimiento curado y memoria conversacional.

**Diseño propuesto:**

#### 1. Nuevo flag en registry: `atomic: bool` (default false)
Más sencillo que un `type` nuevo, retrocompatible con `type=knowledge_base`. Activación opcional por KB.
```json
{
  "name": "ai-lab-memory",
  "type": "knowledge_base",
  "atomic": true,        // bypass curator + NLM, embed 1:1
  "exclude_patterns": ["MEMORY.md"],
  "content_hash": true,
  "scan_interval_seconds": 86400
}
```

#### 2. Pipeline alternativo cuando `atomic: true`
- **Skip curator**: en `_distill_project` (main.py), si `entry.get("atomic")` → saltar `curate_project` y `compute_tier`. Tier efectivo = "atomic".
- **Embed directo**: cada source `.md` (post `exclude_patterns`) → 1+ chunks Qdrant en colección `nlm_notes`. Reusa el indexer existente con flags.
- **Skip NLM siempre**: no llamar `_distill_nlm`. Si por alguna razón el routing lo pide, cortar antes.
- **No tocar tier_override**: con `atomic: true` el concepto de tier no aplica → ignorar.

#### 3. Metadata enriquecida en payload Qdrant
Cada chunk lleva:
```json
{
  "project": "ai-lab-memory",
  "source": "project_aegis.md",
  "memory_type": "project",        // del frontmatter del .md
  "linked_project": "aegis",       // auto-derivado, ver abajo
  "tier": "atomic",
  ...
}
```

#### 4. Auto-derivación de `linked_project`
- Regex `^(project|feedback)_(.+)\.md$` → captura `aegis`, `env_masker`, etc.
- Match contra el registry actual (`registry.list_projects()`). Si existe → set `linked_project`. Si no → leave empty (sin error, solo no se linka).
- Casos sin link: `MEMORY.md` (excluido), `feedback_baseline_quality.md` (no apunta a un proyecto específico), `max_subscription_proxy.md` (apunta a un sistema, no a un proyecto registrado), etc. — quedan con `linked_project: null` y siguen siendo recuperables vía búsqueda libre.
- Para casos especiales tipo `feedback_avoid_trivy.md` que sí apuntan a un proyecto (`aegis`), considerar regla manual o frontmatter explícito `linked_project: aegis` que tenga prioridad sobre la auto-derivación.

#### 5. Query expansion cuando se filtra por proyecto
En `core/query.py`, cuando una query lleva filtro `project=aegis`:
```python
# antes
filter = {"project": "aegis"}
# después
filter = {"$or": [{"project": "aegis"}, {"linked_project": "aegis"}]}
```
Esto hace que toda query por proyecto traiga **gratis** la memoria asociada. Cero cambios para el caller, máxima ganancia.

### Cambios mínimos en Erudito (Fase 1 actualizada — sustituye la versión original)

| Cambio | Archivo | LOC aprox | Fricción |
|---|---|---|---|
| Flag `atomic: bool` en registry schema + persistencia YAML/Redis | `core/registry.py` | ~10 | #6 |
| Pipeline alternativo `atomic` en `_distill_project` (skip curate, skip NLM, embed directo) | `main.py:_distill_project` | ~30 | #6 |
| Auto-derivación de `linked_project` desde filename + match con registry | `core/indexer.py` o nuevo helper en `core/atomic.py` | ~25 | #6 |
| Query expansion `project OR linked_project` en filtros | `core/query.py:_multi_retrieve` | ~10 | #6 |
| `exclude_patterns: list[str]` por KB | `core/scanner.py` + registry schema | ~15 | #1 |
| Hash de contenido (sha256) opcional, flag `content_hash: bool` | `core/scanner.py:177-193` | ~10 | #2 |
| `scan_interval_seconds: int` por KB, respetado en `_distill_loop` | `main.py:463-473`, registry | ~8 | #3 |
| ~~`tier_override`~~ | — | 0 | descartado, redundante con `atomic: true` |
| ~~`enricher` setdefault de `type`~~ | — | 0 | **descartado, ya funciona** |

**Total: ~108 LOC efectivas + tests + schema docs.** Más grande que las ~40 originales, pero el grueso (#6) es donde está el valor real. Cada chunk es retrocompatible — proyectos existentes siguen igual, solo `ai-lab-memory` (y futuros) usan el nuevo camino.

### Mount strategy — decisión: **Opción A (compose mount)**

Recomendación adoptada. Razones:
- Real-time sync gratis: el host edita los .md, el container los ve inmediatamente
- AutoDream → next Erudito scan funciona automáticamente sin polling ni hooks
- Cero código auxiliar (sin rsync cron, sin hook custom)
- El read-only mount es seguro: Erudito no puede mutar la auto-memory del host
- Único costo: recreate del container (downtime ~30s, healthcheck rápido)

**Cambio concreto:** añadir al `docker-compose.yml` de Erudito:
```yaml
volumes:
  - /home/r0calex/ai-lab/knowledge:/app/knowledge:ro
  - /home/r0calex/ai-lab/erudito/data:/app/data
  - /home/r0calex/.claude/projects/-home-r0calex-ai-lab/memory:/app/memories/ai-lab:ro  # ← nuevo
```

Y re-registrar el path como `/app/memories/ai-lab` (en vez del snapshot actual `/app/knowledge/ai-lab-memory`).

**Cleanup post-Fase 1:** una vez el live mount esté validado, **borrar** el snapshot `~/ai-lab/knowledge/ai-lab-memory/` y re-registrar con el path live. Marcar el snapshot actual con un README explicando que es temporal de Phase 0.

### Plan refinado de fases

- [x] **Fase 0 — Smoke test** ✅ ejecutado 2026-04-08 (resultados arriba)
- [x] **Fase 1 — Implementación atomic + 4 fricciones restantes** ✅ desplegado 2026-04-09 (~230 LOC, ver `IR-2026-04-09-auto-memory-atomic-kb.md`)
- [x] **Fase 1.5 — Compose mount + container recreate** ✅ image `erudito:v3-atomic`, mount `/app/memories/ai-lab` live
- [x] **Fase 2 — Re-registrar auto-memory con config refinada** ✅ 16/16 archivos atomic-indexed, query expansion validado end-to-end
- [ ] **Fase 3 — Cleanup snapshot Phase 0** (borrar `~/ai-lab/knowledge/ai-lab-memory/` — huérfano, safe to delete después de unos días de confianza)
- [ ] **Fase 4 — Extensión a otros proyectos** (auto-memory de `desarrollos_openclaw`, etc.)
- [x] **Fase 5 — Documentación IR** ✅ `IR-2026-04-09-auto-memory-atomic-kb.md` con before/after evidence en `docs/evidence/`. SPEC + SOP pendientes (Fase 5b).

### Phase 1 follow-ups (low priority, no bloqueantes)

Detectados durante la implementación de Phase 1 — no son críticos pero son los siguientes pasos lógicos:

- [ ] **Registrar Aegis/env-masker como proyectos Erudito** para mejorar el ratio de auto-link (3/16 → ~6/16). Aegis especialmente: ya tiene docs en `~/ai-lab/aegis/docs/` y muchas memorias apuntan a él (`feedback_avoid_trivy.md`, `project_aegis.md`).
- [ ] **`DELETE /registry/{project}` y `PATCH /registry/{project}` endpoints** para evitar editar `data/registry.yaml` a mano + bumpear `version:` arriba del valor de Redis. Proceso actual está en el IR pero es frágil — la dual-write Redis/YAML hace que ediciones del yaml sin restart sean ignoradas. Cualquier re-config de un KB existente lo necesita.
- [ ] **Reusar `PROJECT_ALIASES` de `core/query.py` en `_derive_linked_project`** como tercer nivel de fallback (después de explicit frontmatter + filename walk). Cubre casos como `project_kubo_openclaw_model.md` → `openclaw-kubo` (orden invertido en el registry), `project_mesh_improvements.md` → `mesh-monitor`, etc. ~10 LOC.
- [ ] **Frontmatter override `linked_project:`** en archivos de auto-memory donde el filename no deriva el proyecto correcto (ej. `feedback_avoid_trivy.md` → semánticamente pertenece a `aegis` pero "trivy" no es ningún proyecto). Solo necesita editar los .md, sin cambio de código. Cero LOC en Erudito.
- [ ] **Optimización wipe-and-rebuild → incremental upsert** para atomic KBs grandes. Hoy cada delta wipea todos los puntos del proyecto y re-indexa. Para 16 archivos es trivial; para cientos sería desperdicio. Mejorar a: para cada source en delta, delete_by_source + upsert. Phase 2.

### Sinergias desbloqueadas (out-of-scope inmediato pero relevantes)

1. Memoria del mesh **accesible cross-node** desde Kubo y Sariatu vía Erudito API/MCP, sin sincronizar archivos físicos entre nodos
2. Validación cruzada automática: agente trabajando en Aegis puede preguntar a Erudito "¿hay feedback previo sobre supply chain tools?" y recuperar `feedback_avoid_trivy.md` sin carga manual de contexto
3. AutoDream se vuelve más útil — sus consolidaciones ahora tienen lectores río abajo, no solo el Claude Code local
4. Cuando reevaluemos MemPalace en ~2 semanas (`memory/project_mempalace_evaluation.md`), la pregunta cambia de "¿lo necesito?" a "¿qué hace MemPalace que este loop NO cubre?" — probablemente solo ingestión de exports de chat web (Claude.ai, ChatGPT), que sería capa adicional, no reemplazo

### Riesgos a vigilar

- **NLM circuit breaker ya tripped** (ver sección "Tier 1 projects blocked on NLM" arriba): si Fase 0 mete a `ai-lab-memory` como Tier 1 sin override, suma 14 más a la cola de fallos NLM. El tier_override de Fase 1 lo evita, pero Fase 0 puede ensuciar métricas mientras tanto. Considerar curarlo manualmente como T2 desde el inicio si el endpoint lo permite.
- **Path expansion**: `~/.claude/...` requiere expansión. `main.py:300` usa `os.path.expanduser()` — debería funcionar, pero validar en Fase 0.
- **Volumen Qdrant**: 14 archivos chicos no mueven la aguja, pero si extendemos a auto-memory de todos los proyectos puede llegar a cientos de archivos. Evaluar antes de Fase 4.

---

## Nice to Have (backlog)

- [ ] **Coherence check algorithm**: Implement in eval-agent (LLM synthesis + embedding comparison). Spec exists.
- [ ] **Technical docs pipeline**: Second Qdrant use case — index external library docs for offline agent access.
- [ ] **`NLM_SCORE_BOOST` dead code**: Remove or implement in query engine. (review #12)
- [ ] **SSE multi-line parsing**: Handle streaming responses with multiple data lines. (review #13)
- [ ] **Test coverage**: Add unit tests for execute_query, answer_from_registry, circuit breaker, _pull_remote_docs, _distill_llm, _distill_direct, _distill_nlm. (review #18)
- [x] **LLM response validation**: ~~Add `_is_nlm_error` check to `_distill_llm`~~ — Done, guards against error responses from LLM gateway.
- [ ] **Rate limiter on /search**: Prevent Ollama/NLM overload from burst requests. (review #16)
- [ ] **push-docs.sh filename escaping**: Escape `$BASENAME` through json.dumps. (review #17)
- [ ] **Register endpoint for remote projects**: Allow POST /registry without path/git validation when node != hanzo. (review #15)
- [ ] **Dockerfile safe.directory**: Restrict to specific mounted paths instead of '*'. (review #19)
- [ ] **NLM concurrency on HTTP path**: `/curate?force_nlm=true` bypasses the NLM semaphore since it doesn't go through `_bounded_distill`. Add semaphore acquisition in `_distill_project` for NLM calls. (review #21)
- [ ] **Stale nlm_consecutive_failures read**: `_distill_nlm` reads `entry` at top, then increments counter from stale snapshot. Use atomic increment in `update_fields` or re-read before write. (review #22)
