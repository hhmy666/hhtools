/**
 * `{id}_m.bytes` 的 ProtoBuf 编解码。字段号、写零规则、负数编码都按真实 `666_m.bytes` 逆向对齐
 * （见 README「ProtoBuf 格式」一节 + 主玩 LevelSaveData.cs / TileData.cs / TileTowerData.cs / DebuffData.cs）。
 *
 * 规则（protobuf-net 语义）：
 * - 顶层按字段号升序写；Version(1) 为 0 时省略（非 required，等于默认值不写）。
 * - TileData 的 X/Y/Z/TileId（1–4）标了 IsRequired → 始终写（含 0）；
 *   DebuffDataIndex(5) 默认 -1，等于 -1 时省略，≥0（含 0）才写。
 * - int 可为负（随机标记 -10..-23、坐标理论可负）→ 按 64 位补码 varint，占 10 字节。
 * 纯逻辑，node --test 可直接测（decode→encode 对真实文件逐字节还原）。
 */



                           








                            







export const NO_DEBUFF = -1;

// ---------- 写 ----------

class Writer {
          bytes           = [];
  push(b        )       { this.bytes.push(b & 0xff); }
  /** 无符号/有符号 varint：负数按 64 位补码（10 字节），正数最小长度 */
  varint(value        )       {
    let v = BigInt(value);
    if (v < 0n) v += 1n << 64n; // 64 位补码
    for (let i = 0; i < 10; i++) {
      const byte = Number(v & 0x7fn);
      v >>= 7n;
      if (v === 0n) { this.push(byte); return; }
      this.push(byte | 0x80);
    }
  }
  tag(field        , wire        )       { this.varint((field << 3) | wire); }
  varintField(field        , value        )       { this.tag(field, 0); this.varint(value); }
  bytesField(field        , data          )       { this.tag(field, 2); this.varint(data.length); for (const b of data) this.push(b); }
  stringField(field        , s        )       { this.bytesField(field, utf8(s)); }
  take()           { return this.bytes; }
}

function utf8(s        )           {
  return Array.from(new TextEncoder().encode(s));
}

function encodeTile(t          )           {
  const w = new Writer();
  w.varintField(1, t.x);
  w.varintField(2, t.y);
  w.varintField(3, t.z);
  w.varintField(4, t.tileId);
  if (t.debuffIndex !== NO_DEBUFF) w.varintField(5, t.debuffIndex);
  return w.take();
}
function encodeTower(t           )           {
  const w = new Writer();
  w.varintField(1, t.x);
  w.varintField(2, t.y);
  w.varintField(3, t.direction);
  return w.take();
}
function encodeDebuff(d            )           {
  const w = new Writer();
  w.varintField(1, d.debuffId);
  return w.take();
}

/** 编码为 .bytes 字节数组（Uint8Array）。传入的 tiles 顺序即写入顺序——导出前请先 sortTilesForSave */
export function encodeLevelSave(save           )             {
  const w = new Writer();
  if (save.version !== 0) w.varintField(1, save.version);
  w.stringField(2, save.templateId);
  w.stringField(3, save.duplicateId);
  w.varintField(4, save.width);
  w.varintField(5, save.height);
  for (const t of save.tiles) w.bytesField(6, encodeTile(t));
  for (const t of save.towers) w.bytesField(7, encodeTower(t));
  for (const d of save.debuffs) w.bytesField(8, encodeDebuff(d));
  return new Uint8Array(w.take());
}

/** 导出顺序：tiles 按 (Z,Y,X) 升序（主玩 TileData.CompareTo，无 TileId 次关键字）；towers 滤空后原序 */
export function sortTilesForSave                                               (tiles              )      {
  return [...tiles].sort((a, b) => a.z - b.z || a.y - b.y || a.x - b.x);
}

// ---------- 读 ----------

class Reader {
          i = 0;
          buf            ;
  constructor(buf            ) { this.buf = buf; }
  get eof()          { return this.i >= this.buf.length; }
  /** 读一个 varint，返回有符号 number（负数来自 64 位补码） */
  varint()         {
    let shift = 0n, result = 0n, bytes = 0;
    for (;;) {
      const b = this.buf[this.i++] ;
      result |= BigInt(b & 0x7f) << shift;
      bytes++;
      if ((b & 0x80) === 0) break;
      shift += 7n;
    }
    // 10 字节 = 64 位补码的负数；其它一律非负
    if (bytes === 10 && result >= (1n << 63n)) result -= 1n << 64n;
    return Number(result);
  }
  tag()                                  {
    const key = this.varint();
    return { field: key >>> 3, wire: key & 7 };
  }
  lenBytes()             {
    const len = this.varint();
    const out = this.buf.subarray(this.i, this.i + len);
    this.i += len;
    return out;
  }
  skip(wire        )       {
    if (wire === 0) this.varint();
    else if (wire === 2) this.lenBytes();
    else if (wire === 5) this.i += 4;
    else if (wire === 1) this.i += 8;
    else throw new Error('不支持的 wire type ' + wire);
  }
}

function decodeTile(buf            )           {
  const r = new Reader(buf);
  const t           = { x: 0, y: 0, z: 0, tileId: 0, debuffIndex: NO_DEBUFF };
  while (!r.eof) {
    const { field, wire } = r.tag();
    if (wire !== 0) { r.skip(wire); continue; }
    const v = r.varint();
    if (field === 1) t.x = v;
    else if (field === 2) t.y = v;
    else if (field === 3) t.z = v;
    else if (field === 4) t.tileId = v;
    else if (field === 5) t.debuffIndex = v;
  }
  return t;
}
function decodeTower(buf            )            {
  const r = new Reader(buf);
  const t            = { x: 0, y: 0, direction: 0 };
  while (!r.eof) {
    const { field, wire } = r.tag();
    if (wire !== 0) { r.skip(wire); continue; }
    const v = r.varint();
    if (field === 1) t.x = v; else if (field === 2) t.y = v; else if (field === 3) t.direction = v;
  }
  return t;
}
function decodeDebuff(buf            )             {
  const r = new Reader(buf);
  const d             = { debuffId: 0 };
  while (!r.eof) {
    const { field, wire } = r.tag();
    if (wire !== 0) { r.skip(wire); continue; }
    const v = r.varint();
    if (field === 1) d.debuffId = v;
  }
  return d;
}

export function decodeLevelSave(buf            )            {
  const r = new Reader(buf);
  const save            = {
    version: 0, templateId: '', duplicateId: '', width: 0, height: 0,
    tiles: [], towers: [], debuffs: [],
  };
  const dec = new TextDecoder();
  while (!r.eof) {
    const { field, wire } = r.tag();
    if (field === 1 && wire === 0) save.version = r.varint();
    else if (field === 2 && wire === 2) save.templateId = dec.decode(r.lenBytes());
    else if (field === 3 && wire === 2) save.duplicateId = dec.decode(r.lenBytes());
    else if (field === 4 && wire === 0) save.width = r.varint();
    else if (field === 5 && wire === 0) save.height = r.varint();
    else if (field === 6 && wire === 2) save.tiles.push(decodeTile(r.lenBytes()));
    else if (field === 7 && wire === 2) save.towers.push(decodeTower(r.lenBytes()));
    else if (field === 8 && wire === 2) save.debuffs.push(decodeDebuff(r.lenBytes()));
    else r.skip(wire);
  }
  return save;
}
