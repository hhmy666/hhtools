export const strategyLabels = {
  random: '随机点击',
  greedy: '贪心凑组',
  local: '局部最优',
  unlock: '解锁优先',
  pressure: '高压路径',
};
const ratios = (wins, trials) => trials ? wins / trials : 0;

export function prepareSimulation(tiles) {
  const blockers = new Uint32Array(tiles.length);
  const releases = tiles.map(() => []);
  tiles.forEach((tile, index) => {
    tiles.forEach((upper, next) => {
      if (upper.z > tile.z && Math.abs(upper.x - tile.x) < 2 && Math.abs(upper.y - tile.y) < 2) {
        blockers[index]++;
        releases[next].push(index);
      }
    });
  });
  return { tiles, blockers, releases };
}

function better(left, right) {
  for (let i = 0; i < left.length; i++) {
    if (left[i] !== right[i]) return left[i] < right[i];
  }
  return false;
}

export function simulateClear(board, capacity, strategy, random) {
  const {tiles, blockers, releases} = board;
  const remainingBlockers = blockers.slice();
  const open = new Set();
  blockers.forEach((blocked, index) => { if (!blocked) open.add(index); });
  const bag = new Map();
  let size = 0, clicks = 0, peak = 0, firstMatch = null;

  while (open.size && clicks < tiles.length) {
    let next, best;
    if (strategy === 'random') {
      next = [...open][Math.floor(random() * open.size)];
    } else {
      const visibleByType = new Map();
      if (strategy === 'local') {
        for (const index of open) {
          const {tileId} = tiles[index];
          visibleByType.set(tileId, (visibleByType.get(tileId) ?? 0) + 1);
        }
      }
      for (const index of open) {
        const {tileId} = tiles[index];
        const have = bag.get(tileId) ?? 0;
        const match = have === 2;
        const settled = size + 1 - (match ? 3 : 0);
        const newlyOpen = releases[index].filter(i => remainingBlockers[i] === 1);
        const unlocked = newlyOpen.length;
        const unlockedStructure = newlyOpen.filter(i => tiles[i].structure).length;
        const bias = random();
        let score;
        if (strategy === 'greedy') score = [match ? 0 : 1, -have, settled, -unlocked, bias];
        else if (strategy === 'local') score = [settled, match ? 0 : 1, -unlocked, -visibleByType.get(tileId), bias];
        else if (strategy === 'unlock') score = [-unlocked, -unlockedStructure, settled, match ? 0 : 1, bias];
        else score = [-settled, match ? 1 : 0, unlocked, have, bias];
        if (!best || better(score, best)) { best = score; next = index; }
      }
    }
    open.delete(next);
    clicks++;
    const {tileId} = tiles[next];
    const count = (bag.get(tileId) ?? 0) + 1;
    size++;
    if (count === 3) {
      size -= 3;
      if (firstMatch === null) firstMatch = clicks;
      bag.delete(tileId);
    } else {
      bag.set(tileId, count);
    }
    peak = Math.max(peak, size);
    if (size >= capacity) return { won: false, peak, clicks, firstMatch, reason: '背包已满' };
    for (const index of releases[next]) {
      if (--remainingBlockers[index] === 0) open.add(index);
    }
  }
  const won = clicks === tiles.length && size === 0;
  return { won, peak, clicks, firstMatch, reason: won ? '' : '剩余牌无法成组' };
}

export function summarizeRuns(results) {
  const wins = results.reduce((count, {won}) => count + Number(won), 0);
  return {
    wins,
    rate: ratios(wins, results.length),
    peak: Math.max(...results.map(result => result.peak)),
    averageClicks: results.reduce((sum, {clicks}) => sum + clicks, 0) / results.length,
    firstFailure: results.find(result => !result.won),
  };
}
