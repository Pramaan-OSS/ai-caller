function estimatePriceHtml(estimate, escapeHtml) {
  const previous = `<p class="meta">${escapeHtml(estimate.sentence)}</p>`;
  if (estimate.priceTypicalMicro === undefined || estimate.priceTypicalMicro === null) return previous;
  const money = micro => `$${(micro / 1_000_000).toFixed(6)}`;
  return `<p><strong>usually about ${money(estimate.priceTypicalMicro)}</strong><br><small>at most ${money(estimate.priceMaxMicro)}</small></p>${previous}`;
}
