"""One-off: identify which Documents lack a MinerU markdown."""
import pathlib
from api.neo4j_client import run_read_query

cache_dir = pathlib.Path('pipeline/cache/mineru_markdown')


def safe_name(fp: str) -> str:
    return fp.replace('/', '_').replace('\\', '_').replace(':', '_')


rows = run_read_query(
    'MATCH (d:Document) RETURN d.file_path AS fp, d.filename AS fn, d.notice_type AS nt',
    max_rows=10_000,
)
print(f'Total Documents: {len(rows)}')

missing = []
for r in rows:
    if not (cache_dir / f"{safe_name(r['fp'])}.md").exists():
        missing.append(r)

print(f'Missing markdown: {len(missing)}')
print()
exts: dict[str, int] = {}
nt_counts: dict[str, int] = {}
for r in missing:
    fn = r['fn'] or ''
    suf = fn.rsplit('.', 1)[-1].lower() if '.' in fn else '(none)'
    exts[suf] = exts.get(suf, 0) + 1
    nt = r['nt'] or '(none)'
    nt_counts[nt] = nt_counts.get(nt, 0) + 1

print('By extension:')
for k, v in sorted(exts.items(), key=lambda x: -x[1]):
    print(f'  .{k}: {v}')
print()
print('By notice_type:')
for k, v in sorted(nt_counts.items(), key=lambda x: -x[1]):
    print(f'  {k}: {v}')
