import unittest
from hashlib import sha256
from scripts.tests.test_ordinary_add_engine import source
from scripts.tower_reducer.ordinary_add import build_ordinary_add, normalize_ordinary_add_options


class OrdinaryAdviceTests(unittest.TestCase):
    def test_failed_bottom_eighteen_has_verified_applicable_alternatives(self):
        from scripts.tower_reducer.ordinary_add_advice import recommend_ordinary_add
        raw = source([(x,y,z) for z in range(6) for x in
                      ([4,6,10,12] if z%2 == 0 else [5,11]) for y in [4,7,10,13]])
        options = normalize_ordinary_add_options({})
        self.assertFalse(build_ordinary_add(raw, options, 'new')['ok'])
        advice = recommend_ordinary_add(raw, options, 'new')
        self.assertEqual(len(advice['schemes']), 2)
        self.assertEqual(advice['source_sha256'], sha256(raw).hexdigest())
        self.assertEqual(advice['current_options'], options)
        choices = [s['parameters']['ordinary_add'] for s in advice['schemes']]
        self.assertTrue(any(p['added_tiles']==18 and p['placement_strategy']=='both' for p in choices))
        self.assertTrue(any(p['added_tiles']==24 and p['placement_strategy']=='bottom_first' for p in choices))
        for scheme in advice['schemes']:
            p = scheme['parameters']['ordinary_add']
            result = build_ordinary_add(raw, p, 'new')
            self.assertTrue(result['ok'])
            self.assertEqual(scheme['output_sha256'], sha256(result['output']).hexdigest())
            self.assertEqual(scheme['facts']['final_total'], result['summary']['final_total'])
            self.assertEqual(p['tile_type'], options['tile_type'])
            self.assertNotIn('output', scheme)
            self.assertEqual({c['field'] for c in scheme['changes']}, {k for k in options if options[k]!=p[k]})

    def test_does_not_offer_unverified_candidates(self):
        from unittest.mock import patch
        from scripts.tower_reducer.ordinary_add_advice import recommend_ordinary_add
        with patch('scripts.tower_reducer.ordinary_add_advice.build_ordinary_add', return_value={'ok':False}) as build:
            advice = recommend_ordinary_add(source(), {}, 'new')
        self.assertEqual(advice['schemes'], [])
        self.assertLessEqual(build.call_count, 8)
