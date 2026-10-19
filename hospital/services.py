"""Bed, queue and simulation logic used by the Flask routes."""
import json
import random
from datetime import date, timedelta

from . import db, models


def is_live(con):
    """A real hospital runs on the calendar date; the demo runs on its own simulated clock."""
    return db.get_settings(con).get("clock") == "real"


def today(con):
    s = db.get_settings(con)
    if s.get("clock") == "real":
        return date.today()
    return date.fromisoformat(s["today"])


def _log(con, field, on):
    """Count today's arrivals, admissions and discharges for a real hospital (feeds the admissions forecast)."""
    if not is_live(con) or on != today(con):
        return
    d = on.isoformat()
    con.execute("INSERT OR IGNORE INTO daily_log VALUES (?, 0, 0, 0, 0)", (d,))
    con.execute(f"UPDATE daily_log SET {field} = {field} + 1, "
                "occupied = (SELECT COUNT(*) FROM beds WHERE patient_id IS NOT NULL) WHERE day = ?", (d,))


# ---------- waiting list ----------

def priority(p, settings, on):
    waited = (on - date.fromisoformat(p["added_on"])).days
    s = settings
    score = (float(s["w_urgency"]) * p["urgency"]
             + float(s["w_wait"]) * min(waited, float(s["wait_cap"]))
             + float(s["w_prob"]) * (p["admit_prob"] or 0))
    return round(score, 1), waited


def queue(con):
    s, on = db.get_settings(con), today(con)
    rows = []
    for r in con.execute("SELECT * FROM patients WHERE status = 'waiting'"):
        p = db.row_to_dict(r)
        p["priority"], p["days_waited"] = priority(p, s, on)
        rows.append(p)
    rows.sort(key=lambda p: (-p["priority"], p["added_on"]))
    for i, p in enumerate(rows, 1):
        p["rank"] = i
    return rows


def add_patient(con, data, sim_true_los=None):
    clinical = data.get("clinical") or {}
    source = data.get("source", "opd")
    admit_prob = data.get("admit_prob")
    if source == "emergency" and admit_prob is None and clinical:
        admit_prob = models.predict_admit(clinical)["probability"]
    cur = con.execute(
        "INSERT INTO patients (name, age, gender, source, ward, condition, clinical, urgency, admit_prob, status, added_on, sim_true_los)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'waiting', ?, ?)",
        (data["name"], data.get("age"), data.get("gender"), source, data.get("ward"), data.get("condition"),
         json.dumps(clinical), int(data.get("urgency", 1)), admit_prob,
         data.get("added_on") or today(con).isoformat(), sim_true_los))
    _log(con, "arrivals", date.fromisoformat(data.get("added_on") or today(con).isoformat()))
    return cur.lastrowid


def free_bed(con, ward=None):
    q = ("SELECT beds.id FROM beds JOIN wards ON wards.id = beds.ward_id "
         "WHERE beds.patient_id IS NULL AND wards.kind = 'general'")
    if ward:
        r = con.execute(q + " AND wards.name = ? ORDER BY beds.id LIMIT 1", (ward,)).fetchone()
        if r:
            return r["id"]
    r = con.execute(q + " ORDER BY beds.id LIMIT 1").fetchone()
    return r["id"] if r else None


def admit(con, patient_id, bed_id=None, on=None):
    p = con.execute("SELECT * FROM patients WHERE id = ? AND status = 'waiting'", (patient_id,)).fetchone()
    if p is None:
        raise ValueError("patient is not on the waiting list")
    p = db.row_to_dict(p)
    bed_id = bed_id or free_bed(con, p["ward"])
    if bed_id is None:
        raise ValueError("no free bed")
    if con.execute("SELECT patient_id FROM beds WHERE id = ?", (bed_id,)).fetchone()["patient_id"]:
        raise ValueError("bed is occupied")
    p50, p90, _, _ = models.predict_los(p)
    on = on or today(con)
    con.execute("UPDATE patients SET status = 'admitted', admitted_on = ?, los_p50 = ?, los_p90 = ? WHERE id = ?",
                (on.isoformat(), p50, p90, patient_id))
    con.execute("UPDATE beds SET patient_id = ? WHERE id = ?", (patient_id, bed_id))
    _log(con, "admissions", on)
    return {"bed_id": bed_id, "los_p50": p50, "los_p90": p90}


def discharge(con, bed_id, on=None):
    r = con.execute("SELECT patient_id FROM beds WHERE id = ?", (bed_id,)).fetchone()
    if r is None or r["patient_id"] is None:
        raise ValueError("bed is already free")
    on = on or today(con)
    con.execute("UPDATE patients SET status = 'discharged', discharged_on = ? WHERE id = ?", (on.isoformat(), r["patient_id"]))
    con.execute("UPDATE beds SET patient_id = NULL WHERE id = ?", (bed_id,))
    _log(con, "discharges", on)


# ---------- hospital setup (real hospital only) ----------

def wards(con):
    return [dict(r) for r in con.execute(
        "SELECT wards.id, wards.name, COUNT(beds.id) AS beds, COUNT(beds.patient_id) AS occupied "
        "FROM wards LEFT JOIN beds ON beds.ward_id = wards.id GROUP BY wards.id ORDER BY wards.id")]


def add_beds(con, ward_id, count, prefix=None):
    w = con.execute("SELECT name FROM wards WHERE id = ?", (ward_id,)).fetchone()
    if w is None:
        raise ValueError("ward not found")
    if not 1 <= count <= 500:
        raise ValueError("number of beds must be between 1 and 500")
    prefix = (prefix or "".join(x[0] for x in w["name"].split()).upper() or "B").strip()
    taken = {r["label"] for r in con.execute("SELECT label FROM beds WHERE ward_id = ?", (ward_id,))}
    i = added = 0
    while added < count:
        i += 1
        label = f"{prefix}-{i:02d}"
        if label not in taken:
            con.execute("INSERT INTO beds (ward_id, label) VALUES (?, ?)", (ward_id, label))
            added += 1


def add_ward(con, name, beds, prefix=None):
    name = (name or "").strip()
    if not name:
        raise ValueError("ward name is required")
    if con.execute("SELECT 1 FROM wards WHERE lower(name) = lower(?)", (name,)).fetchone():
        raise ValueError("a ward with that name already exists")
    wid = con.execute("INSERT INTO wards (name, kind) VALUES (?, 'general')", (name,)).lastrowid
    if beds:
        add_beds(con, wid, beds, prefix)
    return wid


def delete_ward(con, ward_id):
    if con.execute("SELECT 1 FROM beds WHERE ward_id = ? AND patient_id IS NOT NULL", (ward_id,)).fetchone():
        raise ValueError("discharge the patients in this ward first")
    con.execute("DELETE FROM beds WHERE ward_id = ?", (ward_id,))
    con.execute("DELETE FROM wards WHERE id = ?", (ward_id,))


def delete_bed(con, bed_id):
    r = con.execute("SELECT patient_id FROM beds WHERE id = ?", (bed_id,)).fetchone()
    if r is None:
        raise ValueError("bed not found")
    if r["patient_id"] is not None:
        raise ValueError("discharge the patient first")
    con.execute("DELETE FROM beds WHERE id = ?", (bed_id,))


def register_inpatient(con, bed_id, data):
    """Put a patient who is already in a bed onto the board, with the date they were admitted."""
    r = con.execute("SELECT beds.patient_id, wards.name AS ward FROM beds JOIN wards ON wards.id = beds.ward_id "
                    "WHERE beds.id = ?", (bed_id,)).fetchone()
    if r is None:
        raise ValueError("bed not found")
    if r["patient_id"] is not None:
        raise ValueError("bed is occupied")
    admitted_on = date.fromisoformat(data.get("admitted_on") or today(con).isoformat())
    if admitted_on > today(con):
        raise ValueError("admission date cannot be in the future")
    pid = add_patient(con, {**data, "ward": r["ward"], "added_on": admitted_on.isoformat()})
    return admit(con, pid, bed_id, on=admitted_on)


# ---------- beds and overview ----------

def beds(con):
    on = today(con)
    out = []
    for r in con.execute(
            "SELECT beds.id AS bed_id, beds.label, wards.name AS ward, wards.kind, p.id AS pid, p.name, p.age, "
            "p.gender, p.condition, p.source, p.admitted_on, p.los_p50, p.los_p90 "
            "FROM beds JOIN wards ON wards.id = beds.ward_id LEFT JOIN patients p ON p.id = beds.patient_id "
            "ORDER BY wards.id, beds.id"):
        d = {"bed_id": r["bed_id"], "label": r["label"], "ward": r["ward"], "kind": r["kind"], "patient": None, "status": "free"}
        if r["pid"] is not None:
            adm = date.fromisoformat(r["admitted_on"])
            exp = adm + timedelta(days=round(r["los_p50"]))
            late = adm + timedelta(days=round(r["los_p90"]))
            days_left = (exp - on).days
            d["status"] = "freeing" if days_left <= 1 else "occupied"
            d["patient"] = {"id": r["pid"], "name": r["name"], "age": r["age"], "gender": r["gender"],
                            "condition": r["condition"], "source": r["source"], "admitted_on": r["admitted_on"],
                            "days_in": (on - adm).days, "expected_discharge": exp.isoformat(),
                            "latest_discharge": late.isoformat(), "days_left": days_left,
                            "los_p50": r["los_p50"], "los_p90": r["los_p90"]}
        out.append(d)
    return out


def recent_daily_admissions(con, days=14):
    s = db.get_settings(con)
    if s.get("clock") == "real":
        on = today(con)
        if (on - date.fromisoformat(s.get("created_on", on.isoformat()))).days < days:
            return float(s.get("typical_daily_admissions") or 6)  # not enough history yet: use the setup figure
        total = con.execute("SELECT COALESCE(SUM(arrivals), 0) FROM daily_log WHERE day >= ? AND day < ?",
                            ((on - timedelta(days=days)).isoformat(), on.isoformat())).fetchone()[0]
        return total / days
    rows = con.execute("SELECT admissions, arrivals FROM daily_log ORDER BY day DESC LIMIT ?", (days,)).fetchall()
    if not rows:
        return 6.0
    return sum(r["arrivals"] for r in rows) / len(rows)


def overview(con, days=7):
    on = today(con)
    bl = [b for b in beds(con) if b["kind"] == "general"]
    occupied = [b for b in bl if b["patient"]]
    fc, fc_source = models.forecast_admissions(recent_daily_admissions(con), on + timedelta(days=1), days)
    waiting = con.execute("SELECT COUNT(*) FROM patients WHERE status = 'waiting'").fetchone()[0]
    free_now = len(bl) - len(occupied)
    timeline, free = [], free_now
    for i, f in enumerate(fc, 1):
        d = on + timedelta(days=i)
        leaving = sum(1 for b in occupied if b["patient"]["expected_discharge"] == d.isoformat()
                      or (i == 1 and b["patient"]["expected_discharge"] < d.isoformat()))
        free = free + leaving
        timeline.append({"day": d.isoformat(), "expected_discharges": leaving, "expected_admissions": f["expected"],
                         "beds_free_before_admissions": free})
        free = max(free - f["expected"], 0)
    t1 = timeline[0]
    return {
        "today": on.isoformat(), "beds_total": len(bl), "beds_occupied": len(occupied), "beds_free": free_now,
        "waiting": waiting,
        "tomorrow": {"discharges": t1["expected_discharges"], "admissions": t1["expected_admissions"],
                     "bed_gap": round(t1["beds_free_before_admissions"] - t1["expected_admissions"], 1)},
        "forecast": timeline, "forecast_source": fc_source,
        "by_ward": [{"ward": w, "total": sum(1 for b in bl if b["ward"] == w),
                     "occupied": sum(1 for b in occupied if b["ward"] == w)} for w in dict.fromkeys(b["ward"] for b in bl)],
    }


# ---------- simulation ----------

def next_day(con, rng=None):
    """Advance the demo clock by one day: discharge finished stays, add arrivals, admit from the queue."""
    from .seed_demo import random_patient
    rng = rng or random.Random()
    on = today(con) + timedelta(days=1)
    db.set_setting(con, "today", on.isoformat())

    discharged = 0
    for r in con.execute("SELECT beds.id AS bed_id, p.admitted_on, p.sim_true_los, p.los_p50 FROM beds "
                         "JOIN patients p ON p.id = beds.patient_id").fetchall():
        stay = r["sim_true_los"] or r["los_p50"]
        if (on - date.fromisoformat(r["admitted_on"])).days >= round(stay):
            discharge(con, r["bed_id"], on)
            discharged += 1

    fc, _ = models.forecast_admissions(recent_daily_admissions(con), on, 1)
    arrivals = max(0, round(rng.gauss(fc[0]["expected"], fc[0]["expected"] ** 0.5)))
    for _ in range(arrivals):
        data, true_los = random_patient(rng, on)
        add_patient(con, data, true_los)

    admitted = 0
    for p in queue(con):
        if free_bed(con) is None:
            break
        admit(con, p["id"], on=on)
        admitted += 1
    occupied = con.execute("SELECT COUNT(*) FROM beds WHERE patient_id IS NOT NULL").fetchone()[0]
    con.execute("INSERT OR REPLACE INTO daily_log VALUES (?, ?, ?, ?, ?)", (on.isoformat(), admitted, discharged, arrivals, occupied))
    return {"today": on.isoformat(), "discharged": discharged, "arrivals": arrivals, "admitted": admitted}
