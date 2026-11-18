/**
 * 极简 CSV 读写。游戏配置表（config_client_*.csv / config_level.csv）用逗号分隔、不加引号、
 * 字段里也不会出现换行，所以这里按行拆就够；但导出时仍按 RFC 规矩给含逗号/引号/换行的字段加引号，
 * 免得哪天备注里写了逗号把表冲乱。纯逻辑，node --test 可直接测。
 */

/** 拆一行 CSV：认双引号包裹与 "" 转义；不含引号时就是普通逗号分隔 */
export function splitCsvLine(line        )           {
  const out           = [];
  let field = '';
  let quoted = false;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (quoted) {
      if (c === '"') {
        if (line[i + 1] === '"') { field += '"'; i++; } else quoted = false;
      } else field += c;
    } else if (c === '"') {
      quoted = true;
    } else if (c === ',') {
      out.push(field); field = '';
    } else field += c;
  }
  out.push(field);
  return out;
}

/** 导出时转义一个字段：含逗号/引号/换行才加引号 */
export function escapeCsvField(s        )         {
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

/** 整段文本拆成行（去掉 \r、去掉结尾空行），再逐行拆字段 */
export function parseCsv(text        )             {
  return text
    .replace(/\r\n?/g, '\n')
    .split('\n')
    .filter((l, i, a) => l.length > 0 || i < a.length - 1) // 只丢最末尾的空行
    .filter(l => l.length > 0)
    .map(splitCsvLine);
}

/** 行数组拼成一行 CSV */
export function toCsvLine(fields                   )         {
  return fields.map(f => escapeCsvField(f)).join(',');
}
