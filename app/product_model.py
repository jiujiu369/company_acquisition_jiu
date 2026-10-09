"""Keep the product series separate from an individual SKU specification."""
import re


_JG = re.compile(r'(?<![A-Za-z])JG(?![A-Za-z])', re.I)
_CODE_FAMILY = re.compile(r'(?<![A-Za-z])([A-Za-z]+)[-_]?\d+', re.I)


def product_model(value, title=None, *, manual=False):
    if manual or not isinstance(value, str):
        return value
    families = {family.upper() for family in _CODE_FAMILY.findall(value)}
    if (_JG.search(value) and '国标' in value + str(title or '')
            and not families.difference({'JG'})):
        return '国标JG'
    return value
