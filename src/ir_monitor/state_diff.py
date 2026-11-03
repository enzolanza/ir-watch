"""Compara dois arquivos de estado e diz se algo importante mudou.

O robô grava o estado (state/ir_watch.db) no repositório. Sem esta
comparação, ele gravaria a cada execução, mesmo sem nada novo, só porque
algumas colunas registram "visto agora". Aqui essas colunas são ignoradas:
só conta como mudança um documento novo, um alerta enviado, uma falha nova
ou recuperada, etc.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

#: Tabelas que importam e as colunas que mudam a cada execução sem
#: significar nada (ignoradas na comparação).
TABLES: dict[str, set[str]] = {
    "events": {"updated_at"},
    "source_observations": {"last_seen"},
    "meta": set(),
}


def _snapshot(path: str) -> dict[str, list[tuple]] | None:
    if not Path(path).is_file():
        return None
    out: dict[str, list[tuple]] = {}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as conn:
        for table, ignored in TABLES.items():
            try:
                columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            except sqlite3.DatabaseError:
                return None
            if not columns:
                out[table] = []
                continue
            keep = [c for c in columns if c not in ignored]
            rows = conn.execute(f"SELECT {', '.join(keep)} FROM {table}").fetchall()
            out[table] = sorted(rows, key=repr)
    return out


def state_changed(before: str, after: str) -> bool:
    """True se houve mudança relevante (ou se não dá para comparar)."""
    old, new = _snapshot(before), _snapshot(after)
    if old is None or new is None:
        return True  # na dúvida, grava
    return old != new
