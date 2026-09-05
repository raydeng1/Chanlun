/* 调试：笔丢弃原因、背驰比例分布、三买缺失 */
const http = require('http');
global.window = global;
require('./static/js/chanlun.js');

function get(path) {
  return new Promise((res, rej) => {
    http.get({ host: '127.0.0.1', port: 8770, path, timeout: 30000 }, r => {
      let d = ''; r.on('data', c => d += c);
      r.on('end', () => { try { res(JSON.parse(d)); } catch (e) { rej(e); } });
    }).on('error', rej);
  });
}

(async () => {
  const d = await get('/api/kline?code=300750&period=day&fqt=1&limit=600');
  const bars = d.bars;

  // --- 1. 复现 buildBis 内部，统计丢弃原因
  const merged = Chan.mergeKline(bars);
  const fx = Chan.findFractals(merged, bars);

  // 复制 alternate 逻辑
  function alternate(fx) {
    const seq = [];
    for (const f of fx) {
      const last = seq[seq.length - 1];
      if (last && last.type === f.type) {
        if ((f.type === 1 && f.h > last.h) || (f.type === -1 && f.l < last.l)) seq[seq.length - 1] = f;
      } else seq.push(f);
    }
    return seq;
  }
  const seq0 = alternate(fx);
  console.log(`原始分型 ${fx.length} → 交替后 ${seq0.length}`);

  // 统计：相邻交替分型有多少满足"方向自洽"
  let okCnt = 0, badCnt = 0, gapCnt = 0;
  const bads = [];
  for (let i = 0; i < seq0.length - 1; i++) {
    const a = seq0[i], b = seq0[i + 1];
    if (b.i - a.i < 4) { gapCnt++; continue; }
    const dir = b.type === 1 ? 1 : -1;
    const p1 = a.type === -1 ? a.l : a.h;
    const p2 = b.type === 1 ? b.h : b.l;
    const ok = dir === 1 ? p2 > p1 : p2 < p1;
    if (ok) okCnt++;
    else { badCnt++; if (bads.length < 8) bads.push({ i, aType: a.type, bType: b.type, ai: a.i, bi: b.i, aT: a.t, bT: b.t, p1: +p1.toFixed(2), p2: +p2.toFixed(2), dir }); }
  }
  console.log(`交替序列相邻对：方向自洽 ${okCnt}，方向矛盾 ${badCnt}，间距不足 ${gapCnt}`);
  console.log('方向矛盾样本：');
  bads.forEach(x => console.log('   ', JSON.stringify(x)));

  // --- 2. 背驰 area 比例分布
  const r = Chan.analyze(bars, { fxGap: 4, bcRatio: 0.85 });
  console.log(`\n笔 ${r.bis.length}，中枢 ${r.zss.length}`);
  const ratios = [];
  for (let i = 2; i < r.bis.length; i++) {
    const a = r.bis[i - 2], mid = r.bis[i - 1], b = r.bis[i];
    const zg = Math.min(a.high, mid.high, b.high), zd = Math.max(a.low, mid.low, b.low);
    if (a.dir === -1 && mid.dir === 1 && b.dir === -1 && zg > zd && b.low < a.low) {
      ratios.push({ ratio: +(b.power.area / a.power.area).toFixed(3), ampRatio: +(b.power.amp / a.power.amp).toFixed(3), t: b.t2 });
    }
  }
  console.log('底背驰候选 area 比例：', JSON.stringify(ratios.slice(0, 12)));

  // --- 3. 中枢与后续笔（三买条件）
  console.log('\n中枢明细：');
  r.zss.forEach((z, i) => {
    const o1 = r.bis[z.endBi + 1], o2 = r.bis[z.endBi + 2];
    console.log(`  中枢${i}: ZD ${z.zd.toFixed(2)}~ZG ${z.zg.toFixed(2)} 笔[${z.startBi}..${z.endBi}] count=${z.count} ` +
      `| 后续笔 ${o1 ? (o1.dir > 0 ? '上' : '下') + ' h=' + o1.high.toFixed(2) + ' l=' + o1.low.toFixed(2) : '无'}` +
      `, ${o2 ? (o2.dir > 0 ? '上' : '下') + ' h=' + o2.high.toFixed(2) + ' l=' + o2.low.toFixed(2) : '无'}`);
  });
  console.log('\n信号：', r.points.map(p => `${p.t} ${p.label}`).join(' | '));
})();
