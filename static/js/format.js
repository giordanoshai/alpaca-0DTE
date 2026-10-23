/* ----------------------------------------------------------------------------
 * 展示层格式化：全是纯函数，不碰 DOM、不碰状态
 *
 * 拿后端字段算出卡片上那一行字。放在一起是因为它们共享同一条纪律 ——
 * 拿不到数据就如实显示横杠，绝不拿 0 或空字符串冒充一个确定的数字。
 * --------------------------------------------------------------------------*/

const VWAP_DISTANCE_UI_LIMIT = 99.9;

function parseVwapDistance(value) {
  const parsed = Number(String(value).replace('−', '-').replace(/^[≥≤]/, ''));
  return Number.isFinite(parsed) ? parsed : null;
}

function formatVwapDistanceForUi(value) {
  const parsed = parseVwapDistance(value);
  if (parsed == null) return value;
  if (parsed > VWAP_DISTANCE_UI_LIMIT) return '≥99.9';
  if (parsed < -VWAP_DISTANCE_UI_LIMIT) return '≤−99.9';
  return value;
}

// 两侧都被拦时，卡片显示哪一条理由。
//
// 2026-09-01 起这里没有特判了：方向闸（rule 名 `mtf`，只拦一侧）与动能闸删除后，
// 剩下的规则全部与方向无关，两侧要么都放行、要么撞同一堵墙，取哪个都一样。
// 原先那段特判是为了避免「卡片叫用户去买 PUT、PUT 按钮却是灰的」（2026-08-26
// 09:40 的 META / GOOGL 现场）。铁律本身仍然成立 —— 「只能买 X」这句话只有在 X
// 真能买时才可以说 —— 只是现在没有任何一条规则会说这句话。
// 守卫见 tests/test_frontend_block_reason_truthful.py。
function pickBlockReason(gl, gs) {
  return (gl && gl.reason) || (gs && gs.reason) || '';
}

// 没有合约可展示时，卡片副标题要说清是「在选」还是「选不出来」。
// 数据全部来自后端 option_candidates.snapshot()：status / error / gate_allowed / gate.reason。
const CANDIDATE_NOTE_MAXLEN = 22;

function candidateNote(preview, held) {
  if (held) return ' —';
  if (!preview) return ' —';
  const clip = (s) => {
    const text = String(s).trim();
    if (!text) return null;
    return ' · ' + (text.length > CANDIDATE_NOTE_MAXLEN
      ? text.slice(0, CANDIDATE_NOTE_MAXLEN - 1) + '…'
      : text);
  };
  // DISCOVERING 是正常的排队等待（并发上限 2），不是错误，别报成失败。
  if (preview.status === 'DISCOVERING') return ' · 选约中…';
  // ERROR 才是真选不出来，原因由后端给（如「无合格期权候选」）。
  if (preview.status === 'ERROR' && preview.error) return clip(preview.error) || ' · 选约失败';
  if (preview.status === 'ERROR') return ' · 选约失败';
  // 闸门本来就不让入场时，合约为空是预期结果，显示闸门理由而不是空横杠。
  if (preview.gate_allowed === false) {
    const reason = preview.gate && preview.gate.reason;
    if (reason) return clip(reason) || ' —';
  }
  if (preview.error) return clip(preview.error) || ' —';
  return ' —';
}


// OCC 期权代码解析（如 QQQ  260117C00612000）：{type: CALL/PUT, typeText: 'C612'}
function parseOptionCode(code) {
  if (!code) return { type: '', typeText: '' };
  const m = code.match(/^([a-zA-Z]+)\s*(\d{6})([CP])(\d{8})$/i);
  if (m) {
    const type = m[3].toUpperCase() === 'C' ? 'CALL' : 'PUT';
    const strike = parseInt(m[4], 10) / 1000;
    return { type, typeText: (type === 'CALL' ? 'C' : 'P') + strike };
  }
  const m2 = code.match(/([CP])\d{8}$/i);
  const type = m2 ? (m2[1].toUpperCase() === 'C' ? 'CALL' : 'PUT') : '';
  return { type, typeText: type };
}

export {
  parseVwapDistance,
  formatVwapDistanceForUi,
  pickBlockReason,
  candidateNote,
  parseOptionCode,
};
