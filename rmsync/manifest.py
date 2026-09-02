"""SQLite manifest tracking synced documents, per-author CrdtId counters and generated ink blocks."""
import json
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    uuid TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    md_path TEXT NOT NULL UNIQUE,
    page_uuid TEXT NOT NULL,
    synced_md TEXT
);
CREATE TABLE IF NOT EXISTS pages (
    doc_uuid TEXT NOT NULL,
    page_uuid TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (doc_uuid, page_uuid)
);
CREATE TABLE IF NOT EXISTS authors (
    page_uuid TEXT NOT NULL,
    author INTEGER NOT NULL,
    next_id INTEGER NOT NULL,
    PRIMARY KEY (page_uuid, author)
);
CREATE TABLE IF NOT EXISTS gen_blocks (
    page_uuid TEXT NOT NULL,
    group_node_part2 INTEGER NOT NULL,
    source_hash TEXT NOT NULL,
    source TEXT NOT NULL,
    anchor_start_part1 INTEGER NOT NULL,
    anchor_start_part2 INTEGER NOT NULL,
    reserved_ids_json TEXT NOT NULL,
    n_lines INTEGER NOT NULL,
    group_item_part2 INTEGER NOT NULL,
    PRIMARY KEY (page_uuid, group_node_part2)
);
"""
GEN_COLS = ("group_node_part2", "source_hash", "source", "anchor_start_part1", "anchor_start_part2",
            "reserved_ids", "n_lines", "group_item_part2")


class Manifest:
    def __init__(self, path="sync.db"):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        try:   # migrate pre-synced_md databases
            self.db.execute("ALTER TABLE documents ADD COLUMN synced_md TEXT")
        except sqlite3.OperationalError:
            pass

    def close(self):
        self.db.close()

    # documents
    def upsert_document(self, uuid: str, name: str, md_path: str, page_uuid: str, synced_md: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO documents(uuid,name,md_path,page_uuid,synced_md) VALUES(?,?,?,?,?) "
                "ON CONFLICT(uuid) DO UPDATE SET name=excluded.name, md_path=excluded.md_path, "
                "page_uuid=excluded.page_uuid, synced_md=excluded.synced_md", (uuid, name, md_path, page_uuid, synced_md))

    def get_document(self, uuid: str | None = None, md_path: str | None = None) -> dict | None:
        if uuid is not None:
            row = self.db.execute("SELECT * FROM documents WHERE uuid=?", (uuid,)).fetchone()
        elif md_path is not None:
            row = self.db.execute("SELECT * FROM documents WHERE md_path=?", (md_path,)).fetchone()
        else:
            raise ValueError("uuid or md_path required")
        return dict(row) if row else None

    # pages
    def set_pages(self, doc_uuid: str, page_uuids: list[str]) -> None:
        with self.db:
            self.db.execute("DELETE FROM pages WHERE doc_uuid=?", (doc_uuid,))
            self.db.executemany("INSERT INTO pages VALUES(?,?,?)",
                                [(doc_uuid, p, i) for i, p in enumerate(page_uuids)])

    def list_pages(self, doc_uuid: str) -> list[str]:
        rows = self.db.execute("SELECT page_uuid FROM pages WHERE doc_uuid=? ORDER BY position", (doc_uuid,)).fetchall()
        return [r["page_uuid"] for r in rows]

    # authors / id counters
    def next_id(self, page_uuid: str, author: int) -> int:
        row = self.db.execute("SELECT next_id FROM authors WHERE page_uuid=? AND author=?",
                              (page_uuid, author)).fetchone()
        return row["next_id"] if row else 1

    # generated blocks
    def list_gen_blocks(self, page_uuid: str) -> list[dict]:
        rows = self.db.execute("SELECT * FROM gen_blocks WHERE page_uuid=? ORDER BY anchor_start_part2",
                               (page_uuid,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["reserved_ids"] = json.loads(d.pop("reserved_ids_json"))
            out.append(d)
        return out

    def commit_page(self, page_uuid: str, author: int, next_id: int, gen_blocks: list[dict]) -> None:
        """Atomically replace the page's id counter and gen_blocks rows (dicts with GEN_COLS keys)."""
        with self.db:
            self.db.execute("INSERT INTO authors(page_uuid,author,next_id) VALUES(?,?,?) "
                            "ON CONFLICT(page_uuid,author) DO UPDATE SET next_id=excluded.next_id",
                            (page_uuid, author, next_id))
            self.db.execute("DELETE FROM gen_blocks WHERE page_uuid=?", (page_uuid,))
            for g in gen_blocks:
                self.db.execute(
                    "INSERT INTO gen_blocks VALUES(?,?,?,?,?,?,?,?,?)",
                    (page_uuid, g["group_node_part2"], g["source_hash"], g["source"], g["anchor_start_part1"],
                     g["anchor_start_part2"], json.dumps(g["reserved_ids"]), g["n_lines"], g["group_item_part2"]))
