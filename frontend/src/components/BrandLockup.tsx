/** FinByte Nexus brand lockup: teal logo tile (the FB monogram) with the two-line wordmark. Assets live in /public/brand. */
export function BrandLockup({ tagline = false }: { tagline?: boolean }) {
  return (
    <div className="brand-block">
      <div className="brand-lockup" role="img" aria-label="FinByte Nexus">
        <span className="brand-symbol"><img src="/brand/icon-128.png" alt="" width={38} height={38} decoding="async" /></span>
        <span className="brand-text"><b>FinByte</b><i>NEXUS</i></span>
      </div>
      {tagline && <>
        <p className="brand-tagline">Intelligence Beyond the Trade.</p>
        <p className="brand-sub">A unified intelligence platform for trading, investing and market research.</p>
        <ul className="brand-chips" aria-label="Capabilities"><li className="chip-green">AI Research</li><li className="chip-blue">Quant Analytics</li><li className="chip-green">Market Intelligence</li></ul>
      </>}
    </div>
  );
}
