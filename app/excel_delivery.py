"""One workbook per product; multiple workbooks travel in a ZIP."""
import io
import zipfile

from app.excel_export import excel_bytes
from app.excel_images import main_image_bytes
from app.product_package import excel_filename


XLSX_MIME = 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'


def excel_download(data_dir, products, documents):
    if not products:
        raise ValueError('尚无可导出的商品')
    files = []
    used = set()
    for product in products:
        document = documents[product.local_product_id]
        name = excel_filename(product, document)
        base, suffix = name[:-5], 2
        while name.casefold() in used:
            name = f'{base}_{suffix}.xlsx'
            suffix += 1
        used.add(name.casefold())
        image = main_image_bytes(data_dir, product, document)
        images = {document['source_product_id']: image} if image else None
        files.append((name, excel_bytes([document], main_images=images)))
    if len(files) == 1:
        return files[0][1], files[0][0], XLSX_MIME
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in files:
            archive.writestr(name, content)
    return output.getvalue(), '商品Excel_逐商品.zip', 'application/zip'
