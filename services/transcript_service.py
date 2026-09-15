import sys
import database


def _get_db():
    app_mod = sys.modules.get("app")
    return getattr(app_mod, "database", database) if app_mod else database


def normalize_student_info(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return {}
    return {
        "student_name": str(raw.get("student_name") or raw.get("xsjbxx_xm") or "").strip(),
        "student_no": str(raw.get("student_no") or raw.get("xsjbxx_xh") or "").strip(),
        "student_class": str(raw.get("student_class") or raw.get("xsjbxx_bjxx") or raw.get("xsjbxx_bjmc") or raw.get("xsjbxx_bj") or "").strip(),
        "student_college": str(raw.get("student_college") or raw.get("xsjbxx_yxxx") or raw.get("xsjbxx_yxmc") or raw.get("xsjbxx_yx") or "").strip(),
        "student_major": str(raw.get("student_major") or raw.get("xsjbxx_zyxx") or raw.get("xsjbxx_zymc") or raw.get("xsjbxx_zy") or "").strip(),
        "student_status": str(raw.get("student_status") or raw.get("xsjbxx_xsdqzt") or raw.get("xsjbxx_xssfzx") or raw.get("xsjbxx_xjztmc") or "").strip(),
    }


def save_academic_transcript(user_id: int, pdf_url: str, student_info: dict = None, max_keep: int = 3) -> dict:
    info = normalize_student_info(student_info or {})
    db = _get_db()
    row_id = db.execute(
        """
        INSERT INTO academic_transcripts
            (user_id, student_name, student_no, student_class,
             student_college, student_major, student_status, pdf_url)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            user_id,
            info.get("student_name") or "",
            info.get("student_no") or "",
            info.get("student_class") or "",
            info.get("student_college") or "",
            info.get("student_major") or "",
            info.get("student_status") or "",
            str(pdf_url or ""),
        ),
    )
    prune_academic_transcripts(user_id, max_keep=max_keep)
    return fetch_academic_transcript_by_id(row_id)


def prune_academic_transcripts(user_id: int, max_keep: int = 3) -> None:
    db = _get_db()
    excess_rows = db.fetch_all(
        """
        SELECT id FROM academic_transcripts
        WHERE user_id = %s
        ORDER BY id DESC
        LIMIT 100 OFFSET %s
        """,
        (user_id, max_keep),
    )
    if excess_rows:
        ids_to_del = [r["id"] for r in excess_rows]
        placeholders = ",".join(["%s"] * len(ids_to_del))
        db.execute(
            f"DELETE FROM academic_transcripts WHERE id IN ({placeholders}) AND user_id = %s",
            (*ids_to_del, user_id),
        )


def fetch_recent_academic_transcripts(user_id: int, limit: int = 3) -> list:
    db = _get_db()
    rows = db.fetch_all(
        """
        SELECT id, user_id, student_name, student_no, student_class,
               student_college, student_major, student_status, pdf_url, created_at
        FROM academic_transcripts
        WHERE user_id = %s
        ORDER BY id DESC
        LIMIT %s
        """,
        (user_id, limit),
    )
    for r in rows:
        if r.get("created_at"):
            r["created_at"] = str(r["created_at"])
    return rows


def fetch_academic_transcript_by_id(record_id: int):
    db = _get_db()
    row = db.fetch_one(
        """
        SELECT id, user_id, student_name, student_no, student_class,
               student_college, student_major, student_status, pdf_url, created_at
        FROM academic_transcripts
        WHERE id = %s
        """,
        (record_id,),
    )
    if row and row.get("created_at"):
        row["created_at"] = str(row["created_at"])
    return row
