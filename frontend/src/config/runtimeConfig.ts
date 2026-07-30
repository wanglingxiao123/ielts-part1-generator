/**
 * Runtime configuration, fetched from /config.json at startup (design.md §9).
 *
 * Deliberately NOT import.meta.env: one image must run in every environment,
 * so environment differences cannot be baked into the bundle.
 */

export interface Thresholds {
  /**
   * Gap-CV above this shows a yellow flag. Heuristic, uncalibrated.
   *
   * Raised from design.md's 0.45/0.70 to 0.75/1.15 after measuring the real
   * balanced fixture: blueprint_valid × material_valid gives CV 0.63, so at
   * CV_FAIL 0.70 both the balanced AND the clustered variant (CV 1.11) score
   * ~0 uniformity and the metric stops discriminating. design.md itself flags
   * these three numbers as having no real-exam baseline; this is a better
   * heuristic, still not a calibration (prd Open Question 1).
   */
  CV_WARN: number
  /** CV that scores uniformity 0. */
  CV_FAIL: number
  /** Points within this many TURN indexes count as one cluster. */
  CLUSTER_SPAN: number
  /** Minimum points in a run before it is called a cluster. */
  CLUSTER_MIN_POINTS: number
  /** Annotation displacement (px) past which a card is "not where it belongs". */
  CLUSTER_DISP_PX: number
  /** form_group turn span above this is flagged as too wide to answer. */
  GROUP_SPAN_WARN: number
  /** Score gap below this is labelled "not significant". */
  SCORE_DIFF_SIGNIFICANT: number
  /** Only dimensions differing by at least this are listed in compare. */
  DIMENSION_DIFF_SHOWN: number
  /**
   * false until thresholds are calibrated against 10-20 real materials
   * (prd Open Question 1). While false the UI labels uniformity as a
   * reference value and never asserts "not uniform" as a conclusion.
   */
  CALIBRATED: boolean
}

/**
 * No `auth` section, deliberately.
 *
 * The Cognito shape (authority / clientId / cognitoDomain / scope / devBypass)
 * is gone: authentication is a same-origin HttpOnly session cookie issued by the
 * web tier, which needs no client-side configuration at all — there is nothing an
 * operator could get wrong here, and no flag that could ship a fake session.
 */
export interface RuntimeConfig {
  contractVersion: string
  apiBaseUrl: string
  thresholds: Thresholds
  /**
   * `perMaterialWallSeconds` is what is left of the old
   * `{ maxBatch, hardLimitSeconds, warnAtSeconds }`.
   *
   * All three described a per-BATCH wall, which the fan-out abolished: `web/fanout.py` sends one
   * AgentCore invocation per material, so the platform's 15-minute limit bounds one ~150-230s
   * material. `maxBatch` therefore has nothing to cap, and `warnAtSeconds: 720` would have fired
   * a "接近 15 分钟上限" warning on a 20-set batch that was running perfectly normally — the
   * progress page now compares against the measured estimate instead
   * (`domain/batchEstimate.ts`).
   *
   * The per-material figure survives because it is still real, and it is still the number that
   * decides whether a single material's refills can finish.
   */
  limits: { perMaterialWallSeconds: number }
  /**
   * `syntheticAudio` is gone. It stood in for a synthesis endpoint that did not exist, with
   * locally generated tone clips, back when /invocations accepted only `generate` and
   * `list_scenarios`. `preview_audio`, `select`, `audio_status` and `presign_audio` all exist now
   * and return real Polly audio, so the flag has nothing left to substitute for — and a flag that
   * can quietly serve tones where a reviewer expects speech is a way to approve a material nobody
   * has actually heard.
   */
  flags: {
    audioWebaudio: boolean
  }
}

export const FALLBACK_CONFIG: RuntimeConfig = {
  contractVersion: '1',
  apiBaseUrl: '/api',
  thresholds: {
    CV_WARN: 0.75,
    CV_FAIL: 1.15,
    CLUSTER_SPAN: 3,
    CLUSTER_MIN_POINTS: 3,
    CLUSTER_DISP_PX: 24,
    GROUP_SPAN_WARN: 12,
    SCORE_DIFF_SIGNIFICANT: 5,
    DIMENSION_DIFF_SHOWN: 3,
    CALIBRATED: false,
  },
  limits: { perMaterialWallSeconds: 900 },
  flags: { audioWebaudio: false },
}

let current: RuntimeConfig = FALLBACK_CONFIG

export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  try {
    const res = await fetch('/config.json', { cache: 'no-store' })
    if (!res.ok) throw new Error(`config.json ${res.status}`)
    const body = (await res.json()) as Partial<RuntimeConfig>
    // Lenient merge ("宽进严出", design.md §10): a config missing a new key
    // degrades to the fallback value instead of crashing the app.
    current = {
      ...FALLBACK_CONFIG,
      ...body,
      thresholds: { ...FALLBACK_CONFIG.thresholds, ...body.thresholds },
      limits: { ...FALLBACK_CONFIG.limits, ...body.limits },
      flags: { ...FALLBACK_CONFIG.flags, ...body.flags },
    }
  } catch (err) {
    console.warn('[config] falling back to built-in defaults:', err)
    current = FALLBACK_CONFIG
  }
  return current
}

export function getConfig(): RuntimeConfig {
  return current
}

export function getThresholds(): Thresholds {
  return current.thresholds
}
