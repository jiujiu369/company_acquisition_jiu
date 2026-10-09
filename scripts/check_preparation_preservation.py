"""Read-only production preservation audit against the pre-change file inventory."""
import hashlib
import json
import sqlite3
from pathlib import Path


def main():
    root=Path(__file__).resolve().parents[1]
    backup=root/'reports/preparation-backup-20261005-112406'
    inventory=json.loads((backup/'historical-files.json').read_text(encoding='utf-8'))
    failures=[]
    for row in inventory:
        path=root/row['path']
        if not path.is_file() or path.stat().st_size!=row['size']:
            failures.append({'path':row['path'],'reason':'missing or size changed'})
            continue
        with path.open('rb') as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=row['sha256']:
                failures.append({'path':row['path'],'reason':'hash changed'})
    counts={}
    with sqlite3.connect((root/'data/database/app.db').as_uri()+'?mode=ro',uri=True) as connection:
        for table in ('products','product_assets','product_captures','collection_jobs','tasks'):
            counts[table]=connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0]
        integrity=connection.execute('PRAGMA integrity_check').fetchone()[0]
    expected={'products':60,'product_assets':3501,'product_captures':77,'collection_jobs':219,'tasks':234}
    report={'files_checked':len(inventory),'changed_files':failures,'counts':counts,
        'expected_counts':expected,'counts_unchanged':counts==expected,'integrity':integrity,
        'session':'This implementation never started a production collection browser or modified its profile.'}
    (root/'reports/preparation-preservation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'files_checked':len(inventory),'changed_files':len(failures),'counts_unchanged':counts==expected,'integrity':integrity}))
    assert not failures and counts==expected and integrity=='ok'


if __name__=='__main__':
    main()
