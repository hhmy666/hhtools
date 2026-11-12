"""Server-proven manual edits recalculate density without increase-only guards."""
import csv
import io
from pathlib import Path
import tempfile
import unittest
from xml.etree import ElementTree as ET
import zipfile

from scripts.tests.test_parameter_config import FIELDS, csv_bytes, entry, source_row
from scripts.tower_reducer.parameter_config import ParameterConfigService


class ManualParameterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir='/private/tmp')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / 'config'
        self.config.mkdir()
        self.service = ParameterConfigService(self.root)
        self.write(['6|001|+100|3', '5|007|099|2'] + ['4|008|80|2'] * 8)

    def write(self, values):
        self.raw = csv_bytes(FIELDS, [source_row(parameters=values)])
        (self.config / 'config_level.csv').write_bytes(self.raw)

    def calculate(self, source=(18,18), result=(15,18), **fields):
        return self.service.calculate(self.config,[entry(source,result,
            operation='manual',new_template_id='manualTpl',**fields)])

    def test_delete_three_recalculates_first_density_and_translates_remaining_nine(self):
        row = self.calculate()['rows'][0]
        self.assertEqual(row['status'],'ready',row)
        self.assertEqual(row['new_parameters'],['5|001|+100|3','4|007|099|2']+['3|008|80|2']*8)
        self.assertEqual(row['delta'],{'A':-1,'B':0})
        self.assertEqual((row['original_total'],row['final_total'],row['added_tiles']),(36,33,-3))
        self.assertEqual((self.config/'config_level.csv').read_bytes(),self.raw)

    def test_equal_total_recolor_keeps_exact_half_up_and_first_delta(self):
        self.write(['3|001|+100|3','2|007|099|2']+['2|008|80|2']*8)
        row = self.calculate(result=(15,21))['rows'][0]
        self.assertEqual(row['status'],'ready',row)
        self.assertEqual(row['new_parameters'],['3|001|+100|4','2|007|099|3']+['2|008|80|3']*8)
        self.assertEqual(row['delta'],{'A':0,'B':1})
        self.assertEqual(row['added_tiles'],0)

    def test_manual_can_grow_while_one_type_decreases(self):
        row = self.calculate(result=(15,24))['rows'][0]
        self.assertEqual(row['status'],'ready',row)
        self.assertEqual(row['new_parameters'],['5|001|+100|4','4|007|099|3']+['3|008|80|3']*8)
        self.assertEqual(row['added_tiles'],3)

    def test_manual_new_b_uses_confirmed_new_a_rule_and_ordinary_name_priority(self):
        self.write(['10|0|100|0','9|7|100|0']+['8|8|99|0']*8)
        row = self.calculate(source=(60,0),result=(57,18))['rows'][0]
        self.assertEqual(row['status'],'ready',row)
        self.assertEqual(row['new_parameters'],['10|0|100|3','9|7|100|3']+['8|8|99|3']*8)
        self.assertEqual(row['parameter_basis'],{'B':'new_A_groups_per_pattern_ceiling_minus_one_min_three'})
        ordinary = self.calculate(source=(60,0),result=(57,18),template_mode='ordinary_add')['rows'][0]
        self.assertEqual(ordinary['new_level_id'],'moreBold1')

    def test_manual_rejects_nontriples_and_legacy_guards_remain_strict(self):
        row = self.calculate(result=(17,18))['rows'][0]
        self.assertEqual(row['status'],'failed')
        self.assertIn('3的倍数',row['message'])
        for operation,counts in (('reduce',(15,18)),('increase',(15,24))):
            row = self.service.calculate(self.config,[entry(source=(18,18),result=counts,operation=operation)])['rows'][0]
            self.assertEqual(row['status'],'failed',row)

    def test_csv_and_excel_label_manual_operation_as_tweak(self):
        calculated = self.calculate()
        raw,_,_ = self.service.export(calculated['calculation_id'],'csv')
        rows = list(csv.reader(io.StringIO(raw.decode('utf-8-sig'))))
        self.assertEqual(dict(zip(rows[0],rows[1]))['操作模式'],'微调')
        raw,_,_ = self.service.export(calculated['calculation_id'],'xlsx')
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        ns = {'s':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        self.assertEqual(sheet.find('.//s:c[@r="J2"]/s:is/s:t',ns).text,'微调')
