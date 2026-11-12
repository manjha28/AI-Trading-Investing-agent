/** What the scalp_hold parameters imply, recomputed live. Mirrors the engine's formulas (scalp_hold/barriers.py); the engine stays authoritative. */
export type GeometryInputs = { cost_rt_pct: number | null; sigma_H_median_pct: number | null; typical_bar_pct: number | null; tick_pct?: number | null };
export type EngineValues = { r: number; k_vol: number; theta_feeR: number; s_cap_mult: number; must_work_frac: number; must_work_at_frac: number; H_minutes: number; bar_minutes: number };

export type Geometry = {
  costRtPct: number | null; floorPct: number | null; volStopPct: number | null; stopPct: number | null; targetPct: number | null; pStar: number | null;
  mustWorkPct: number | null; mustWorkBars: number; stopBars: number | null; mustWorkInBars: number | null; typicalCapped: boolean | null; capThresholdSigmaPct: number | null;
};

const fin = (x: number | null | undefined): x is number => typeof x === 'number' && Number.isFinite(x);

export function geometry(v: EngineValues, g: GeometryInputs): Geometry {
  const costRtPct = fin(g.cost_rt_pct) ? g.cost_rt_pct : null;
  const floorPct = costRtPct !== null && v.theta_feeR > 0 ? costRtPct / v.theta_feeR : null;
  const volStopPct = fin(g.sigma_H_median_pct) && v.r > 0 ? (v.k_vol * g.sigma_H_median_pct) / v.r : null;
  const candidates = [volStopPct, floorPct].filter(fin);
  const stopPct = candidates.length ? Math.max(...candidates) : null;
  const targetPct = stopPct !== null ? v.r * stopPct : null;
  const pStar = stopPct !== null && costRtPct !== null && stopPct > 0 ? (1 + costRtPct / stopPct) / (v.r + 1) : null;
  const mustWorkPct = targetPct !== null ? v.must_work_frac * targetPct : null;
  const mustWorkBars = Math.max(1, Math.ceil((v.H_minutes * v.must_work_at_frac) / v.bar_minutes - 1e-9));
  const typical = fin(g.typical_bar_pct) && g.typical_bar_pct > 0 ? g.typical_bar_pct : null;
  const typicalCapped = volStopPct !== null && floorPct !== null ? floorPct > v.s_cap_mult * volStopPct : null;
  // a signal survives the cap only when sigma_H >= floor x r / (k_vol x s_cap_mult)
  const capThresholdSigmaPct = floorPct !== null && v.k_vol > 0 && v.s_cap_mult > 0 ? (floorPct * v.r) / (v.k_vol * v.s_cap_mult) : null;
  return {
    costRtPct, floorPct, volStopPct, stopPct, targetPct, pStar, mustWorkPct, mustWorkBars, typicalCapped, capThresholdSigmaPct,
    stopBars: stopPct !== null && typical !== null ? stopPct / typical : null,
    mustWorkInBars: mustWorkPct !== null && typical !== null ? mustWorkPct / typical : null,
  };
}

export const pct = (x: number | null, d = 4) => (x === null ? '—' : `${x.toFixed(d)}%`);
export const bars = (x: number | null, d = 1) => (x === null ? '—' : `≈ ${x.toFixed(d)} typical bars`);
