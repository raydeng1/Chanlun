/* 算法验证：从本地服务取真实K线 → 跑缠论 → 结构一致性校验 + 统计 */
const http = require('http');

global.window = global;
require('./static/js/chanlun.js');

const HOST = '127.0.0.1', PORT = 8770;

function get(path) {
  return new Promise((res, rej) => {
    http.get({ host: HOST, port: PORT, path, timeout: 30000 }, r => {
      let d = '';
      r.on('data', c => d += c);
      r.on('end', () => { try { res(JSON.parse(d)); } catch (e) { rej(new Error('bad json: ' + d.slice(0, 120))); } });
    }).on('error', rej);
  });
}

function check(bars, res) {
  const err = [];
  const n = bars.length;

  // 笔：方向交替 + 首尾相接 + 索引递增 + 极值自洽
  for (let i = 0; i < res.bis.length; i++) {
    const b = res.bis[i];
    if (i > 0) {
      if (b.dir === res.bis[i - 1].dir) err.push(`笔${i} 方向未交替`);
      if (b.i1 !== res.bis[i - 1].i2) err.push(`笔${i} 未首尾相接 ${res.bis[i - 1].i2}->${b.i1}`);
    }
    if (!(b.i2 > b.i1)) err.push(`笔${i} 索引非递增 ${b.i1}->${b.i2}`);
    if (b.i2 >= n || b.i1 < 0) err.push(`笔${i} 索引越界 ${b.i1}->${b.i2}/${n}`);
    // 端点价格必须落在对应K线的真实高低点范围内
    const k1 = bars[b.i1], k2 = bars[b.i2];
    if (k1 && (b.p1 < k1.l - 1e-6 || b.p1 > k1.h + 1e-6)) err.push(`笔${i} 起点价 ${b.p1} 不在K线[${k1.l},${k1.h}]`);
    if (k2 && (b.p2 < k2.l - 1e-6 || b.p2 > k2.h + 1e-6)) err.push(`笔${i} 终点价 ${b.p2} 不在K线[${k2.l},${k2.h}]`);
    if (b.dir > 0 && !(b.p2 > b.p1)) err.push(`笔${i} 向上笔终点未抬高`);
    if (b.dir < 0 && !(b.p2 < b.p1)) err.push(`笔${i} 向下笔终点未降低`);
  }

  // 线段
  for (let i = 0; i < res.duans.length; i++) {
    const d = res.duans[i];
    if (!(d.i2 > d.i1)) err.push(`段${i} 索引非递增`);
    if (i > 0 && d.i1 < res.duans[i - 1].i1) err.push(`段${i} 时间倒退`);
    if (d.i2 >= n) err.push(`段${i} 越界`);
  }

  // 中枢：区间有效 + 时间有序 + 不重叠过度
  for (let i = 0; i < res.zss.length; i++) {
    const z = res.zss[i];
    if (!(z.zg > z.zd)) err.push(`中枢${i} 区间无效 zg=${z.zg} zd=${z.zd}`);
    if (!(z.i2 > z.i1)) err.push(`中枢${i} 索引异常`);
    if (z.i2 >= n) err.push(`中枢${i} 越界`);
    if (z.count < 3) err.push(`中枢${i} 构成笔数不足 ${z.count}`);
    if (i > 0 && z.i1 < res.zss[i - 1].i1) err.push(`中枢${i} 起始时间倒退`);
  }

  // 买卖点
  const valid = { '1B': 1, '1S': 1, '2B': 1, '2S': 1, '3B': 1, '3S': 1 };
  for (const p of res.points) {
    if (!valid[p.type]) err.push(`未知信号类型 ${p.type}`);
    if (p.i < 0 || p.i >= n) err.push(`信号 ${p.type} 索引越界 ${p.i}`);
    const k = bars[p.i];
    if (k && (p.price < k.l - 1e-6 || p.price > k.h + 1e-6)) err.push(`信号 ${p.type} 价格 ${p.price} 越界`);
  }
  return err;
}

async function main() {
  const cases = [
    ['600519', 'day', '贵州茅台 日线'],
    ['600519', 'week', '贵州茅台 周线'],
    ['600519', 'month', '贵州茅台 月线'],
    ['600519', '30m', '贵州茅台 30分'],
    ['600519', '1m', '贵州茅台 1分'],
    ['000001', 'day', '上证指数 日线'],
    ['300750', 'day', '宁德时代 日线'],
    ['000858', '60m', '五粮液 60分'],
    ['688981', 'day', '中芯国际 日线'],
    ['601318', 'week', '中国平安 周线'],
  ];

  let allErr = 0, totalMs = 0;
  console.log('周期对齐校验 · 缠论结构一致性测试');
  console.log('='.repeat(76));

  for (const [code, period, label] of cases) {
    let d;
    try {
      d = await get(`/api/kline?code=${code}&period=${period}&fqt=1&limit=600`);
    } catch (e) { console.log(`${label}: 取数失败 ${e.message}`); allErr++; continue; }
    if (!d.bars) { console.log(`${label}: ${JSON.stringify(d).slice(0, 100)}`); allErr++; continue; }

    const bars = d.bars;
    const t0 = Date.now();
    const r = Chan.analyze(bars, { minBars: 5, bcRatio: 0.85 });
    const ms = Date.now() - t0;
    totalMs += ms;

    const err = check(bars, r);
    allErr += err.length;
    const dist = {};
    r.points.forEach(p => dist[p.type] = (dist[p.type] || 0) + 1);

    console.log(`${label.padEnd(16)} K=${String(bars.length).padStart(4)} | 合并${String(r.merged.length).padStart(4)} 分型${String(r.fractals.length).padStart(4)} 笔${String(r.bis.length).padStart(3)} 段${String(r.duans.length).padStart(3)} 中枢${String(r.zss.length).padStart(3)} 信号${String(r.points.length).padStart(3)} | ${String(ms).padStart(3)}ms`);
    console.log(`   信号分布 ${JSON.stringify(dist)}  ${err.length ? '❌ ' + err.slice(0, 4).join(' ; ') : '✅ 通过'}`);
  }

  // 参数敏感性
  console.log('-'.repeat(76));
  const d = await get('/api/kline?code=600519&period=day&fqt=1&limit=600');
  console.log('参数敏感性（600519 日线）：');
  for (const gap of [5, 7, 9, 13]) {
    const r = Chan.analyze(d.bars, { minBars: gap, bcRatio: 0.85 });
    console.log(`  minBars=${gap}  笔 ${r.bis.length}  段 ${r.duans.length}  中枢 ${r.zss.length}  信号 ${r.points.length}`);
  }
  for (const bc of [0.6, 0.75, 0.85, 0.95]) {
    const r = Chan.analyze(d.bars, { minBars: 5, bcRatio: bc });
    console.log(`  bcRatio=${bc}  信号 ${r.points.length}`);
  }

  // 边界：超短数据
  const tiny = d.bars.slice(0, 10);
  const rt = Chan.analyze(tiny, {});
  console.log(`  超短样本(10根) 笔${rt.bis.length} 段${rt.duans.length} 中枢${rt.zss.length} 信号${rt.points.length} → ${rt.bis.length === 0 ? '✅ 安全降级' : '⚠️'}`);

  console.log('='.repeat(76));
  console.log(allErr === 0 ? `✅ 全部通过，总耗时 ${totalMs}ms` : `❌ 共 ${allErr} 处问题`);
  process.exit(allErr === 0 ? 0 : 1);
}

main().catch(e => { console.error('FATAL', e); process.exit(1); });
