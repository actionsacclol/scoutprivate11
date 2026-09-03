/**
 * modelMeta — the single source of truth for per-engine model names, backtest
 * accuracy and approach notes shown across the UI (Terminal MODEL RACK, About
 * MODELS table, Models engine room). Nothing else in src/ hardcodes these —
 * the numbers drifted twice when three copies existed.
 *
 * RUNTIME OVERRIDE: GET /api/models serves each engine's live `model` name and
 * a `backtest` metrics blob; useModelMeta() merges those over the fallbacks.
 * The table below is only what renders in demo mode / backend-down — when an
 * engine is bumped on the backend (e.g. nhl-elo-v1 → v2), the UI follows
 * automatically, and the matching ONE line below should be updated to keep the
 * offline fallback honest.
 */
import { useEffect, useState } from 'react'
import type { Sport } from './types'
import { SPORTS } from './types'
import { fetchModels, type ModelInfo } from './api'

export interface ModelMetaEntry {
  /** Engine identifier, e.g. 'nba-elo-v1'. */
  model: string
  /** Backtested moneyline accuracy, percent (0–100) — historical replay. */
  acc: number
  /** One-line approach description (the About table's APPROACH column). */
  note: string
}

/** A resolved row — fallback values, possibly overridden by /api/models. */
export interface ResolvedModelMeta extends ModelMetaEntry {
  /** True when this row came from GET /api/models (vs the baked fallback). */
  live: boolean
}

export type ModelMetaMap = Record<Sport, ResolvedModelMeta>

// One line per engine — edit here and every surface updates.
export const FALLBACK_MODEL_META: Record<Sport, ModelMetaEntry> = {
  nba: { model: 'nba-elo-v1', acc: 67.8, note: 'Elo · MOV + rolling scoring' },
  nhl: { model: 'nhl-elo-v1', acc: 53.6, note: 'Elo + Poisson goals' }, // v2 bump lands on this line
  nfl: { model: 'nfl-xgb-v3', acc: 67.5, note: 'Elo + QB + XGBoost blend' },
  ncaaf: { model: 'ncaaf-kalman-v2', acc: 75.9, note: 'Kalman margin filter · blowouts count' },
  ncaab: { model: 'ncaab-kalman-v2', acc: 72.0, note: 'Kalman margin filter · D1 only' },
  mlb: { model: 'mlb-elo-v3', acc: 55.5, note: 'Elo + pitcher EWMA' },
  wnba: { model: 'wnba-elo-v1', acc: 66.3, note: 'Elo · expansion-aware' },
  soccer: { model: 'soccer-poisson-v2', acc: 48.9, note: 'Dixon-Coles-lite · 3-way' },
  tennis: { model: 'tennis-elo-v1', acc: 63.8, note: 'surface-blended Elo' },
}

/** Backtest accuracy (%) out of an engine's /api/models `backtest` blob.
 *  Engines encode it as a 0..1 fraction or a 0..100 percent under a few key
 *  names — tolerate all of them. Null when the blob is absent/unreadable. */
export function backtestAccFromBlob(
  bt: Record<string, unknown> | null | undefined,
): number | null {
  const raw = bt ? (bt['accuracy'] ?? bt['acc'] ?? bt['win_rate']) : undefined
  if (typeof raw === 'number' && Number.isFinite(raw)) return raw <= 1 ? raw * 100 : raw
  return null
}

/** Backtest accuracy (%) for one ModelInfo — engine blob when present,
 *  replay fallback when not. (Moved here from ModelsPage so every consumer
 *  shares the one decode.) */
export function backtestAcc(m: ModelInfo | null | undefined, sport: Sport): number {
  return backtestAccFromBlob(m?.backtest) ?? FALLBACK_MODEL_META[sport].acc
}

function fallbackMap(): ModelMetaMap {
  const map = {} as ModelMetaMap
  for (const s of SPORTS) map[s] = { ...FALLBACK_MODEL_META[s], live: false }
  return map
}

/** Merge one /api/models payload over the fallbacks: live model name wins,
 *  blob accuracy wins when decodable, notes always come from the fallback
 *  table (the API doesn't describe approaches). */
export function mergeModelMeta(models: ModelInfo[]): ModelMetaMap {
  const map = fallbackMap()
  for (const m of models) {
    const s = m.sport as Sport
    const fb = FALLBACK_MODEL_META[s]
    if (!fb) continue // unknown sport in the payload — ignore
    map[s] = {
      model: m.model || fb.model,
      acc: backtestAccFromBlob(m.backtest) ?? fb.acc,
      note: fb.note,
      live: true,
    }
  }
  return map
}

/**
 * useModelMeta — per-sport model name + backtest accuracy for display.
 * demo=true (or fetch failure / endpoint absent) → baked fallbacks, live=false.
 * Fetches /api/models ONCE on mount — these values change on retrains, not
 * minute-to-minute, so no polling.
 */
export function useModelMeta(demo: boolean): ModelMetaMap {
  const [map, setMap] = useState<ModelMetaMap>(fallbackMap)
  useEffect(() => {
    if (demo) {
      setMap(fallbackMap())
      return
    }
    let alive = true
    void fetchModels().then((r) => {
      if (alive && r) setMap(mergeModelMeta(r.models))
    })
    return () => {
      alive = false
    }
  }, [demo])
  return map
}
