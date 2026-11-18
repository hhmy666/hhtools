
























export function resolveDraftStorage(getStorage                    )                      {
  try {
    return getStorage();
  } catch {
    return null;
  }
}

const isRecord = (value         )                                   =>
  typeof value === 'object' && value !== null;
const isNumber = (value         )                  =>
  typeof value === 'number' && Number.isFinite(value);

function isDupPara(value         )                   {
  return isRecord(value) && isNumber(value.a) && isNumber(value.seed) && isNumber(value.lucky) && isNumber(value.b);
}

function isMaster(value         )                  {
  if (!isRecord(value) || typeof value.templateId !== 'string' || !isNumber(value.version)
    || !isNumber(value.width) || !isNumber(value.height) || !Array.isArray(value.tiles)
    || !Array.isArray(value.towers) || !Array.isArray(value.debuffs)) return false;
  const tilesOk = value.tiles.every(tile => isRecord(tile) && isNumber(tile.x) && isNumber(tile.y)
    && isNumber(tile.z) && isNumber(tile.tileId) && isNumber(tile.debuffIndex) && isNumber(tile.groupId));
  const towersOk = value.towers.every(tower => isRecord(tower) && isNumber(tower.x)
    && isNumber(tower.y) && isNumber(tower.direction));
  const debuffsOk = value.debuffs.every(debuff => isRecord(debuff) && isNumber(debuff.debuffId));
  return tilesOk && towersOk && debuffsOk;
}

function isDraftData(value         )                                {
  return isRecord(value) && isMaster(value.master) && typeof value.currentLevelId === 'string'
    && Array.isArray(value.dup) && value.dup.length === 10
    && value.dup.every(item => item === null || isDupPara(item))
    && typeof value.strategyGroupId === 'string' && typeof value.randomSeedGroupId === 'string'
    && isNumber(value.levelNum) && isNumber(value.tray);
}

export function encodeLevelEditorDraft(data                      , savedAt = Date.now())         {
  return JSON.stringify({ version: 1, savedAt, data }                                 );
}

export function decodeLevelEditorDraft(raw        )                                {
  try {
    const value          = JSON.parse(raw);
    if (!isRecord(value) || value.version !== 1 || !isNumber(value.savedAt) || !isDraftData(value.data)) return null;
    return value                                     ;
  } catch {
    return null;
  }
}

export function readLevelEditorDraft(storage              , key        )                                {
  try {
    const raw = storage.getItem(key);
    return raw === null ? null : decodeLevelEditorDraft(raw);
  } catch {
    return null;
  }
}

export function writeLevelEditorDraft(
  storage              ,
  key        ,
  data                      ,
  savedAt = Date.now(),
)          {
  try {
    storage.setItem(key, encodeLevelEditorDraft(data, savedAt));
    return true;
  } catch {
    return false;
  }
}

export function removeLevelEditorDraft(storage              , key        )          {
  try {
    storage.removeItem(key);
    return true;
  } catch {
    return false;
  }
}
