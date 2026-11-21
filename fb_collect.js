// Facebook Marketplace collector. Runs inside a logged-in FB search page (built-in browser).
// Search URL: https://www.facebook.com/marketplace/amsterdam/search?query=<q>&maxPrice=200&daysSinceListed=7&sortBy=creation_time_descend&radius=15&exact=false
// Per query, inline this whole file (FB's CSP blocks eval) and then:
//   JSON.stringify(await mhRun({tag:'<q>', scrolls:15, maxPrice:200, towns:'<config.local_towns>', reset:<true on first query>}))
// At the end: JSON.stringify(Object.values(JSON.parse(localStorage.getItem('mh_fb_run'))))
//   -> data/fb_raw.json -> python -I hunt.py fbimport data/fb_raw.json
// Lazy loading needs the page to render: with the Claude window minimized only the first cards load.
window.mhRun = async function (o) {
  if (o.reset) localStorage.removeItem('mh_fb_run');
  const S = new Map(); let last = 0, same = 0;
  for (let k = 0; k < (o.scrolls || 15); k++) {
    const as = document.querySelectorAll('a[href*="/marketplace/item/"]');
    as.forEach(a => {
      const id = (a.href.match(/item\/(\d+)/) || [])[1];
      if (id && !S.has(id)) S.set(id, a.innerText.split('\n').map(s => s.trim()).filter(Boolean));
    });
    if (as.length) as[as.length - 1].scrollIntoView({ block: 'end' });
    window.scrollTo(0, document.documentElement.scrollHeight);
    await new Promise(r => setTimeout(r, 1500));
    if (S.size === last) { if (++same >= 4) break; } else { same = 0; last = S.size; }
  }
  const town = new RegExp('^(' + o.towns + ')\\b', 'i');
  const isPrice = s => /^(€\s?[\d.,]+|[\d.,]+\s?€|gratis|free)$/i.test(s);
  const badge = s => /^(just listed|recién publicado|nuevo|new|vendido|sold|pendiente|pending)$/i.test(s);
  const num = s => { const m = (s || '').match(/[\d.,]+/); return m ? parseFloat(m[0].replace(/\./g, '').replace(',', '.')) : null; };
  const run = JSON.parse(localStorage.getItem('mh_fb_run') || '{}');
  let kept = 0;
  S.forEach((segs, id) => {
    const prices = segs.filter(isPrice), rest = segs.filter(s => !isPrice(s) && !badge(s));
    const title = rest[0] || '', city = rest[rest.length - 1] || '';
    const price = /gratis|free/i.test(prices[0] || '') ? 0 : num(prices[0]);
    if (!town.test(city) || (price != null && price > o.maxPrice) || run[id]) return;
    run[id] = { id, title, price, was: num(prices[1]), city, q: o.tag }; kept++;
  });
  localStorage.setItem('mh_fb_run', JSON.stringify(run));
  return { tag: o.tag, cards: S.size, kept, total: Object.keys(run).length };
};
