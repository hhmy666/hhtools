/**
 * D3 母版派生（等价确定性预览）。给母版套一组 DupPara + 第几次玩 → 现算出这次要玩的牌面。
 * 不追求与 Unity CustomRandom 逐 bit 一致（已与用户确认用等价预览）：随机标记位（-10..-23）按
 * TileTypeCountA 定的种类数、用「种子+第几次玩」确定性洗牌分配类型键；固定 tile 保留原 tileId。
 * 同参数同种子 → 同牌面。规则搬自 playbook assignRandomTypes。
 */
import {assignRandomTypes, makeRng, isRandomMarker} from './logic.js';



/** 派生出来的随机类型用这个段表示，避免与真实 tileId（1000+）相撞；colorOf 据此单独上色 */
export const VARIANT_BASE = 2_000_000;

export function isVariantType(tileId        )          { return tileId >= VARIANT_BASE; }
export function variantKey(tileId        )         { return tileId - VARIANT_BASE; }

/**
 * 把母版派生成一份「这次玩」的 Master：随机标记位替换成具体类型键（VARIANT_BASE+k），其余不动。
 * poolSize/kinds = max(1, A+B)；同一次玩内 random 位按确定性洗牌分配，组间尽量均匀（3 个一组）。
 */
export function deriveMaster(master        , dup         , playIndex        )         {
  const randomIdx           = [];
  master.tiles.forEach((t, i) => { if (isRandomMarker(t.tileId)) randomIdx.push(i); });
  const kinds = Math.max(1, dup.a + Math.max(0, dup.b));
  const rng = makeRng((dup.seed | 0) * 100003 + playIndex * 131 + 1);
  const keys = assignRandomTypes(randomIdx.length, kinds, dup.a > 0 ? dup.a : kinds, rng);

  const tiles = master.tiles.slice();
  randomIdx.forEach((ti, k) => {
    tiles[ti] = { ...tiles[ti] , tileId: VARIANT_BASE + (keys[k] ?? 0) };
  });
  return { ...master, tiles };
}
