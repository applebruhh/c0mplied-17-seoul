"""Narrow, data-preserving SQLite upgrade for the Phase 3 UNKNOWN enum."""
from sqlalchemy import MetaData
from sqlalchemy.schema import CreateTable

from app.tables import Base, Payment


def upgrade_authorization_unknown(engine):
    # SQLite cannot ALTER a CHECK constraint. Rebuild only payments, retaining
    # columns, unique constraints, indexes, IDs and all referring rows.
    raw = engine.raw_connection()
    cursor = raw.cursor()
    try:
        ddl = cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='payments'").fetchone()
        if not ddl or "'UNKNOWN'" in ddl[0].split("CONSTRAINT authorizationstatus", 1)[-1].split(")", 1)[0]:
            return
        metadata = MetaData()
        for table in Base.metadata.sorted_tables:
            if table.name != "payments":
                table.to_metadata(metadata)
        replacement = Payment.__table__.to_metadata(metadata, name="payments_phase3")
        columns = ", ".join(f'"{column.name}"' for column in Payment.__table__.columns)
        cursor.execute("PRAGMA foreign_keys = OFF")
        cursor.execute("BEGIN IMMEDIATE")
        definitions = cursor.execute(
            "SELECT sql FROM sqlite_master WHERE tbl_name='payments' "
            "AND type IN ('index','trigger') AND sql IS NOT NULL"
        ).fetchall()
        cursor.execute(str(CreateTable(replacement).compile(dialect=engine.dialect)))
        cursor.execute(f'INSERT INTO payments_phase3 ({columns}) SELECT {columns} FROM payments')
        cursor.execute("DROP TABLE payments")
        cursor.execute("ALTER TABLE payments_phase3 RENAME TO payments")
        for definition, in definitions:
            cursor.execute(definition)
        if cursor.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Phase 3 migration failed foreign-key validation")
        raw.commit()
    except BaseException:
        raw.rollback()
        raise
    finally:
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.close()
        raw.close()
