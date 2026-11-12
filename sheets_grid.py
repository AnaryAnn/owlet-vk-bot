"""Read literal input data through spreadsheets.get, preserving order and blanks.

Computed values aren't requested: the production statistics workbook currently
times out when Google evaluates them. Formula cells fail explicitly instead of
being mistaken for empty data. Other workbooks keep using the normal Values API.
"""
import re


def bounds(a1):
    match = re.fullmatch(r"(?:'((?:[^']|'')+)'|([^!]+))!([A-Z]+)(\d*)(?::([A-Z]+)(\d*))?", a1)
    if not match:
        raise ValueError('Unsupported A1 range')
    quoted, plain, col, row, end_col, end_row = match.groups()
    def column(value):
        result = 0
        for char in value:
            result = result * 26 + ord(char) - ord('A') + 1
        return result - 1
    first_row = int(row) - 1 if row else 0
    return (quoted.replace("''", "'") if quoted else plain,
            first_row, column(col),
            int(end_row) if end_row else (None if end_col or not row else first_row + 1),
            column(end_col or col) + 1)


def grid_ranges(payload, ranges, formatted=False):
    # Google returns sheets in workbook order, not necessarily request order.
    sheets = {s['properties']['title']: s for s in payload.get('sheets', [])}
    output = []
    for a1 in ranges:
        title, r0, c0, r1, c1 = bounds(a1)
        if title not in sheets:
            raise ValueError('Requested sheet is missing from Google response')
        cells = {}
        for block in sheets[title].get('data', []):
            for row_offset, row in enumerate(block.get('rowData', [])):
                r = block.get('startRow', 0) + row_offset
                if r < r0 or (r1 is not None and r >= r1):
                    continue
                for col_offset, cell in enumerate(row.get('values', [])):
                    c = block.get('startColumn', 0) + col_offset
                    if not c0 <= c < c1:
                        continue
                    value = cell.get('userEnteredValue', {})
                    if 'formulaValue' in value:
                        raise ValueError('Formula found in an input-data range; refusing to replace it with an empty value')
                    if 'numberValue' in value:
                        value = value['numberValue']
                    elif 'stringValue' in value:
                        value = value['stringValue']
                    elif 'boolValue' in value:
                        value = value['boolValue']
                    else:
                        value = ''
                    if value != '':
                        cells[r, c] = value
        rows = []
        if cells:
            for r in range(r0, max(r for r, _ in cells) + 1):
                row = [cells.get((r, c), '') for c in range(c0, c1)]
                while row and row[-1] == '':
                    row.pop()
                rows.append(row)
        output.append({'range': a1, 'values': rows})
    return output
