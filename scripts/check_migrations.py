"""
ตรวจไฟล์ Alembic migration ว่าแตะเฉพาะตารางของโปรเจกต์เราเอง (ใส่ใน CI ให้รันทุกครั้ง — รันเองได้)

    python check_migrations.py --versions alembic/versions --models app/models.py
    python check_migrations.py --versions alembic/versions --models app/models/*.py --extra-table legacy_log

เหมาะกับฐานข้อมูลที่ใช้ร่วมกับระบบอื่น — migration ที่ autogenerate แล้วไม่ได้ตรวจ อาจมี op.drop_table / ALTER
ตารางของคนอื่นติดมา สคริปต์นี้ไล่ทุกคำสั่ง op.* ในทุกไฟล์ แล้วล้มทันทีถ้าชื่อตารางไม่ใช่ของเรา
(หรือเป็นชื่อที่อ่านค่าไม่ได้ เช่นมาจากฟังก์ชัน) — "ของเรา" = ชื่อใน __tablename__ ของไฟล์ models + alembic_version
"""
import argparse
import ast
import glob
import re
import sys
from pathlib import Path

# ตำแหน่งชื่อตารางใน argument ของแต่ละคำสั่ง: (ตำแหน่ง positional, ชื่อ keyword)
OP_TABLE_ARGS = {
    "create_table": [(0, "table_name")],
    "drop_table": [(0, "table_name")],
    "rename_table": [(0, "old_table_name"), (1, "new_table_name")],
    "add_column": [(0, "table_name")],
    "drop_column": [(0, "table_name")],
    "alter_column": [(0, "table_name")],
    "batch_alter_table": [(0, "table_name")],
    "create_index": [(1, "table_name")],
    "drop_index": [(None, "table_name")],
    "create_foreign_key": [(1, "source_table"), (2, "referent_table")],
    "create_unique_constraint": [(1, "table_name")],
    "create_check_constraint": [(1, "table_name")],
    "create_primary_key": [(1, "table_name")],
    "drop_constraint": [(1, "table_name")],
    "create_table_comment": [(0, "table_name")],
    "drop_table_comment": [(0, "table_name")],
}
# batch_op.* อยู่ใน with op.batch_alter_table(<ตารางของเรา>) แล้ว — เหลือตรวจแค่ตารางปลายทางของ FK
BATCH_TABLE_ARGS = {"create_foreign_key": [(1, "referent_table")]}
# ชื่อตารางใน SQL ดิบ (op.execute) — รองรับ schema.table และ [schema].[table]
SQL_TABLE_RE = re.compile(r"\b(?:FROM|INTO|UPDATE|TABLE|JOIN|TRUNCATE)\s+\[?(?:\w+\]?\.\[?)?([A-Za-z_]\w*)", re.I)


def owned_tables(model_patterns: list[str], extra: list[str]) -> set[str]:
    files = [Path(f) for pattern in model_patterns for f in glob.glob(pattern, recursive=True)]
    if not files:
        sys.exit(f"ไม่เจอไฟล์ models ตาม {model_patterns}")
    names = {t for f in files for t in re.findall(r"""__tablename__\s*=\s*["']([^"']+)["']""", f.read_text(encoding="utf-8"))}
    return names | {"alembic_version"} | set(extra)


def check_file(path: Path, owned: set[str]) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # ค่าคงที่ระดับไฟล์: NAME = "ตาราง" หรือ NAME = {"ตาราง": ...} / ["ตาราง", ...]
    consts: dict[str, set[str]] = {}
    for n in tree.body:
        if not isinstance(n, ast.Assign):
            continue
        v = n.value
        elts = v.keys if isinstance(v, ast.Dict) else v.elts if isinstance(v, (ast.List, ast.Tuple, ast.Set)) else [v]
        if all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in elts):
            consts.update({t.id: {e.value for e in elts} for t in n.targets if isinstance(t, ast.Name)})
    # ตัวแปรลูป: for table, ... in _TABLES.items() → table มีค่าได้ตามสมาชิกของ _TABLES
    for loop in ast.walk(tree):
        if isinstance(loop, ast.For):
            target = loop.target.elts[0] if isinstance(loop.target, ast.Tuple) else loop.target
            source = next((consts[x.id] for x in ast.walk(loop.iter) if isinstance(x, ast.Name) and x.id in consts), None)
            if isinstance(target, ast.Name) and source is not None:
                consts[target.id] = source
    problems = []

    def values_of(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, ast.Name):
            return consts.get(node.id)
        return None

    def check_name(node, where):
        names = values_of(node)
        if names is None:
            problems.append(f"{where}: อ่านชื่อตารางไม่ได้ ({ast.unparse(node)}) — ใช้ชื่อตารางเป็นข้อความตรงๆ")
        else:
            problems.extend(f"{where}: ตาราง '{name}' ไม่ใช่ของเรา" for name in sorted(names - owned))

    for call in ast.walk(tree):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)):
            continue
        obj, fn = call.func.value.id, call.func.attr
        where = f"{path.name}:{call.lineno} {obj}.{fn}"
        if obj == "op" and fn == "execute":
            sqls = values_of(call.args[0]) if call.args else None
            if sqls is None:
                problems.append(f"{where}: ตรวจ SQL ไม่ได้ — ใช้ข้อความ SQL ตรงๆ")
            else:
                problems += [f"{where}: ตาราง '{t}' ไม่ใช่ของเรา"
                             for sql in sqls for t in SQL_TABLE_RE.findall(sql) if t not in owned]
            continue
        spec = OP_TABLE_ARGS.get(fn) if obj == "op" else BATCH_TABLE_ARGS.get(fn)
        for pos, kw in spec or []:
            node = next((k.value for k in call.keywords if k.arg == kw), None)
            if node is None and pos is not None and pos < len(call.args):
                node = call.args[pos]
            if node is not None:
                check_name(node, where)
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--versions", required=True, help="โฟลเดอร์ alembic/versions")
    parser.add_argument("--models", required=True, nargs="+", help="ไฟล์ models (ใช้ * ได้)")
    parser.add_argument("--extra-table", action="append", default=[], help="ตารางของเราที่ไม่มีใน models")
    args = parser.parse_args()
    owned = owned_tables(args.models, args.extra_table)
    files = sorted(Path(args.versions).glob("*.py"))
    if not files:
        sys.exit(f"ไม่เจอไฟล์ migration ใน {args.versions}")
    problems = [p for f in files for p in check_file(f, owned)]
    for p in problems:
        print(f"❌ {p}")
    if problems:
        print("\nmigration แตะตารางที่ไม่ใช่ของเรา — ฐานข้อมูลใช้ร่วมกับระบบอื่น ห้ามแตะตารางของคนอื่น")
        return 1
    print(f"✅ migration {len(files)} ไฟล์ แตะเฉพาะตารางของเรา ({', '.join(sorted(owned))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
