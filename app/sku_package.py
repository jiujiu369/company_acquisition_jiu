"""Explicit pack sizes from SKU labels, independent of buyer inventory."""
import re

DEFAULT_STOCK = 9999
PACKAGE_SKU_FIELDS = {'name', 'package_quantity', 'package_unit', 'package_price'}
PACKAGE_LABELS = {'name': 'SKU名称', 'package_quantity': '包装数量',
                  'package_unit': '包装单位', 'package_price': '每包价格'}

# Require a count unit: model numbers such as JG10-6 are never pack sizes.
_COUNT = re.compile(r'(?:(?<![\w.\-])|(?<=每包)|(?<=一包))([1-9]\d*)\s*(只|个|件|片|支|枚|颗|粒|条|根|套|对)(?=$|[\s)）\]】/／,，;；·]|装|一包|每包)')
_AMBIGUOUS_COUNT = re.compile(r'\d+\s*(?:只|个|件|片|支|枚|颗|粒|条|根|套|对)?\s*[-~～至/／]\s*\d+\s*(?:只|个|件|片|支|枚|颗|粒|条|根|套|对)')
_NONPACK_PROPERTY = re.compile(r'起订|极数|孔数|针数|电流|电压|尺寸|长度|重量|温度|库存|stock|moq|minimum', re.I)


def package_values(name, properties=None):
    properties = properties if isinstance(properties, dict) else {}
    combined = ' / '.join(str(v) for v in properties.values())
    label = name if isinstance(name, str) and name.strip() else combined
    # skuBase names are flattened property values. Do not re-introduce excluded
    # semantic fields (MOQ, inventory, poles) through this display-only fallback.
    labels = [str(value) for key, value in properties.items() if not _NONPACK_PROPERTY.search(str(key))]
    if label and (not properties or label != combined):
        labels.append(label)
    if any(_AMBIGUOUS_COUNT.search(text) for text in labels):
        return {'name': label or None, 'package_quantity': None, 'package_unit': None}
    sizes = {(int(match[1]), match[2]) for text in labels for match in _COUNT.finditer(text)}
    quantity, unit = next(iter(sizes)) if len(sizes) == 1 else (None, None)
    return {'name': label or None, 'package_quantity': quantity, 'package_unit': unit}
