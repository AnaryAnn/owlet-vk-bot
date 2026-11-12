import unittest
from sheets_grid import bounds, grid_ranges


def grid_payload(ranges, matrices):
    sheets = {}
    for a1, matrix in zip(ranges, matrices):
        title, row, col, _, _ = bounds(a1)
        sheet = sheets.setdefault(title, {'properties': {'title': title}, 'data': []})
        sheet['data'].append({'startRow': row, 'startColumn': col, 'rowData': [
            {'values': [{'userEnteredValue': {
                'boolValue' if isinstance(v, bool) else 'numberValue' if isinstance(v, (int, float)) else 'stringValue': v}}
                for v in r]} for r in matrix]})
    return {'sheets': list(reversed(list(sheets.values())))}


class GridTests(unittest.TestCase):
    def test_range_order_offsets_blanks_and_scalar_types(self):
        ranges = ["'Other'!B2", "'O''Brien'!AA8:AC", "'Other'!A8:D", "'Other'!D2"]
        matrices = [[[100]], [['one', '', 0], [], ['last', False]], [['name', 30, 17]], []]
        result = grid_ranges(grid_payload(ranges, matrices), ranges)
        self.assertEqual([v['values'] for v in result], matrices)

    def test_formula_and_missing_sheet_fail_instead_of_silent_data_loss(self):
        data = grid_payload(["'s'!A2"], [[['x']]])
        data['sheets'][0]['data'][0]['rowData'][0]['values'][0]['userEnteredValue'] = {'formulaValue': '=1+1'}
        with self.assertRaises(ValueError):
            grid_ranges(data, ["'s'!A2"])
        with self.assertRaises(ValueError):
            grid_ranges(data, ["'missing'!A2"])
