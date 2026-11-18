/** Statistics describe decoded template geometry; audit and source binding belong to the server. */





function tileCounts(tiles                     )                         {
  const counts = new Map                ();
  for (const tile of tiles) counts.set(tile.tileId, (counts.get(tile.tileId) ?? 0) + 1);
  return Object.fromEntries([...counts].sort(([a], [b]) => a - b));
}

function layerTileCounts(tiles                     )                         {
  const counts = new Map                ();
  for (const tile of tiles) counts.set(tile.z, (counts.get(tile.z) ?? 0) + 1);
  return Object.fromEntries([...counts].sort(([a], [b]) => a - b));
}

export function computeTemplateStats(save           )                {
  const markers = (lower        , upper        ) => {
    const tiles = save.tiles.filter(tile => tile.tileId >= lower && tile.tileId <= upper);
    return { total: tiles.length, byTileId: tileCounts(tiles) };
  };
  return {
    tileCount: save.tiles.length, layerCount: new Set(save.tiles.map(tile => tile.z)).size,
    layerTileCounts: layerTileCounts(save.tiles),
    tileIdCounts: tileCounts(save.tiles), debuffCount: save.debuffs.length,
    specialStructures: markers(-23, -12), randomMarkers: markers(-23, -10),
    towerCount: save.towers.length,
    towers: save.towers.map((tower, index) => {
      const tiles = save.tiles.filter(tile => tile.x === tower.x && tile.y === tower.y);
      return { index, x: tower.x, y: tower.y, direction: tower.direction, tileCount: tiles.length, tileIdCounts: tileCounts(tiles) };
    }),
  };
}

export function templateStatsStale(stats                                 , sourceSha256               )          {
  return !!stats && stats.sourceSha256 !== sourceSha256;
}

/** Hash the view itself, including its offset/length, exactly as it will be uploaded. */
export async function sha256Bytes(bytes            )                  {
  const hash = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, '0')).join('');
}
