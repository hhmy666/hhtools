export const REQUIRED_LEVEL_CSV_NAMES = [
  'config_level_list.csv',
  'config_level_randomSeed_group.csv',
  'config_level_randomSeed.csv',
  'config_level_strategy_group.csv',
  'config_level.csv',
]         ;








export function validateLevelCsvNames(names                   )                     {
  const required = new Set        (REQUIRED_LEVEL_CSV_NAMES);
  const counts = new Map                ();
  for (const name of names) counts.set(name, (counts.get(name) ?? 0) + 1);
  const missing = REQUIRED_LEVEL_CSV_NAMES.filter(name => !counts.has(name));
  const duplicates = [...counts].filter(([, count]) => count > 1).map(([name]) => name).sort();
  const extras = [...counts.keys()].filter(name => !required.has(name)).sort();
  return { ok: missing.length === 0 && duplicates.length === 0 && extras.length === 0, missing, duplicates, extras };
}
