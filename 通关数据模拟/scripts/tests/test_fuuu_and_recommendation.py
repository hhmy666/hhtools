import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fuuu import enrich
from parameters import options
from scenario_inputs import Entry


class FuuuTest(unittest.TestCase):
    def test_exact_lookup_and_first_profile_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp)
            (config / "config_client_level_fuuu_dup.csv").write_text(
                "LevelFuuuDup,TowerHelpGameProgressParam,TowerHelpBagTileParam\n1,200|80,10|20\n2,80|100,20|60\n3,,\n"
            )
            (config / "config_level_list.csv").write_text(
                "LevelIndex,LevelId,DDAType,DDALevelId1,LevelFuuuDup\n"
                "465,a,1,b,2\n466,d,0,,3\n"
            )
            (config / "config_level.csv").write_text(
                "LevelId,TemplateId\na,xkd141\nb,xkd142\nd,xkd143\n"
            )
            for entry, expected in (
                (Entry("xkd141", 1, None, "a"), (465, 2)),
                (Entry("xkd142", 1, None), (465, 2)),
                (Entry("xkd143", 1, None), (466, 0)),
                (Entry("new-template", 1, None), (None, 1)),
            ):
                resolved, _ = enrich(entry, config)
                self.assertEqual((resolved.level_index, resolved.fuuu), expected)


class CandidateSearchTest(unittest.TestCase):
    def test_ranges_are_groups_per_kind(self):
        level = SimpleNamespace(tiles=[SimpleNamespace(tile_id=-10)] * 36
                               + [SimpleNamespace(tile_id=-11)] * 9)
        candidates = options(level, {"a_min": 3, "a_max": 4, "b_min": 1, "b_max": 1,
                                     "seed_min": 5, "seed_max": 5}, entropy=4)
        self.assertEqual({(p.a_kinds, p.b_kinds, p.seed, p.luck) for p in candidates},
                         {(3, 3, 5, 100), (4, 3, 5, 100)})
