import unittest

from scenario_inputs import Entry, Parameters, parse_entries


class InputsTest(unittest.TestCase):
    def test_two_markdown_tables_keep_order_and_repeat_ids(self):
        pasted = """| xkd141 |
| --- |
| xkd141 |
| mxd0069 |

| 15\\|3\\|100\\|4 |
| --- |
| 16|3|100|4 |
| 12|2|100|4 |
"""
        self.assertEqual(parse_entries(pasted), [
            Entry("xkd141", 1, Parameters(15, 3, 100, 4)),
            Entry("xkd141", 2, Parameters(16, 3, 100, 4)),
            Entry("mxd0069", 1, Parameters(12, 2, 100, 4)),
        ])

    def test_config_level_reads_header_and_sparse_dup_number(self):
        pasted = ("LevelId,Des,TemplateId,RandomType,DupPara1,DupPara3,StrategyGroupId\n"
                  "string,string,string,int,array,array,int\n"
                  "关卡id,备注,模板 id,副本顺序,第1个,第3个,降难策略\n"
                  "online_7,例子,xkd141,0,,15|3|100|4,1003\n"
                  "online_8,例子,xkd141,0,16|3|100|4,,1003\n")
        self.assertEqual(parse_entries(pasted), [
            Entry("xkd141", 3, Parameters(15, 3, 100, 4), "online_7", strategy_id=1003),
            Entry("xkd141", 1, Parameters(16, 3, 100, 4), "online_8", strategy_id=1003),
        ])

    def test_tabular_mixed_rows_are_not_silently_mispaired(self):
        self.assertEqual(parse_entries("xkd141\t15|3|100|4\nmxd0069\n"), [
            Entry("xkd141", 1, Parameters(15, 3, 100, 4)),
            Entry("mxd0069", 1, None),
        ])
        with self.assertRaisesRegex(ValueError, "数量"):
            parse_entries("xkd141\nmxd0069\n\n15|3|100|4")

    def test_uploaded_files_supply_ids_to_parameter_rows(self):
        self.assertEqual(parse_entries("15|3|100|4", ["xkd141"]), [
            Entry("xkd141", 1, Parameters(15, 3, 100, 4)),
        ])
        self.assertEqual(parse_entries("", ["mxd0069", "xkd141"]), [
            Entry("mxd0069", 1, None), Entry("xkd141", 1, None),
        ])

    def test_interleaved_config_and_file_do_not_create_extra_duplicate(self):
        self.assertEqual(parse_entries("xkd141\t15|3|100|4", ["xkd141"]), [
            Entry("xkd141", 1, Parameters(15, 3, 100, 4)),
        ])

    def test_reject_unknown_path_or_invalid_param(self):
        with self.assertRaises(ValueError):
            parse_entries("../xkd141")
        with self.assertRaises(ValueError):
            parse_entries("xkd141\n15|2147483648|100|4")


if __name__ == "__main__":
    unittest.main()
