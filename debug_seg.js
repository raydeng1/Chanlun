/* 调试：线段划分 + 三买三卖触发情况 */
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
  for (const [code, period] of [['600519', 'day'], ['688981', 'day'], ['300750', 'day']]) {
    const d = await get(`/api/kline?code=${code}&period=${period}&fqt=1&limit=640`);
    const r = Chan.analyze(d.bars, { minBars: 5, bcRatio: 0.85 });
    console.log(`\n===== ${code} ${period}  笔=${r.bis.length} 段=${r.duans.length} 中枢=${r.zss.length}`);

    // 段覆盖了多少笔
    console.log('段：', r.duans.map(x => `[${x.startBi}..${x.endBi}]${x.dir > 0 ? '上' : '下'}`).join(' '));

    // 中枢明细 + 三买三卖检查
    console.log('中枢：');
    r.zss.forEach((z, i) => {
      const o1 = r.bis[z.endBi + 1], o2 = r.bis[z.endBi + 2];
      let verdict = '';
      if (!o1 || !o2) verdict = '后续笔不足';
      else if (o1.dir === 1 && o2.dir === -1) {
        verdict = `离开上${o1.high.toFixed(2)}(>${z.zg.toFixed(2)}?${o1.high > z.zg}) 回调低${o2.low.toFixed(2)}(>${z.zg.toFixed(2)}?${o2.low > z.zg})`;
      } else if (o1.dir === -1 && o2.dir === 1) {
        verdict = `离开下${o1.low.toFixed(2)}(<${z.zd.toFixed(2)}?${o1.low < z.zd}) 反弹高${o2.high.toFixed(2)}(<${z.zd.toFixed(2)}?${o2.high < z.zd})`;
      } else verdict = `方向异常 o1=${o1.dir} o2=${o2.dir}`;
      console.log(`  #${i} [${z.startBi}..${z.endBi}] ZD=${z.zd.toFixed(2)} ZG=${z.zg.toFixed(2)} n=${z.count} | ${verdict}`);
    });

    // 统计所有中枢后 "离开-回调" 的价格关系分布
    let c3b = 0, c3s = 0, cand = 0;
    for (const z of r.zss) {
      const o1 = r.bis[z.endBi + 1], o2 = r.bis[z.endBi + 2];
      if (!o1 || !o2) continue;
      cand++;
      if (o1.dir === 1 && o2.dir === -1 && o1.high > z.zg && o2.low > z.zg) c3b++;
      if (o1.dir === -1 && o2.dir === 1 && o1.low < z.zd && o2.high < z.zd) c3s++;
    }
    console.log(`三买候选 ${cand} → 三买 ${c3b}, 三卖 ${c3s}`);
    console.log('信号：', r.points.map(p => `${p.t.slice(5)} ${p.label}`).join(' | ') || '(无)');
  }
})();
