"""Flask API and dashboard for the hospital bed management platform.

Two separate platforms run from one server, each with its own database:
  http://localhost:5000/           demo hospital, pre-filled, simulated clock (data/hospital.db)
  http://localhost:5000/hospital/  your own hospital, empty until you set it up (data/my_hospital.db)

Run:  python app.py
"""
from datetime import date
from pathlib import Path

from flask import Blueprint, Flask, jsonify, render_template, request

from hospital import db, models, services
from hospital.seed_demo import seed


def error(msg, code=400):
    return jsonify({"error": msg}), code


def init_live(con):
    db.init(con)
    for k, v in {"clock": "real", "created_on": date.today().isoformat(),
                 "hospital_name": "", "typical_daily_admissions": "6"}.items():
        con.execute("INSERT OR IGNORE INTO settings VALUES (?, ?)", (k, v))


def platform(name, prefix, db_path: Path, live: bool):
    """All pages and API routes for one hospital, stored in its own SQLite file."""
    bp = Blueprint(name, __name__, url_prefix=prefix or None)
    session = lambda: db.session(db_path)

    if not db_path.exists():
        with session() as con:
            init_live(con) if live else seed(con)

    @bp.get("/")
    def index():
        meta = models.triage_meta() or {}
        with session() as con:
            s = db.get_settings(con)
            ward_names = [w["name"] for w in services.wards(con)]
        return render_template("index.html", wards=ward_names, complaints=meta.get("complaints", []),
                               arrival_modes=meta.get("categories", {}).get("arrivalmode", []),
                               live=live, base=prefix, hospital_name=s.get("hospital_name") or "")

    @bp.get("/api/overview")
    def overview():
        with session() as con:
            return jsonify(services.overview(con, int(request.args.get("days", 7))))

    @bp.get("/api/queue")
    def get_queue():
        with session() as con:
            return jsonify({"queue": services.queue(con), "settings": db.get_settings(con)})

    @bp.post("/api/queue")
    def add_to_queue():
        data = request.get_json(silent=True) or {}
        if not data.get("name"):
            return error("name is required")
        with session() as con:
            pid = services.add_patient(con, data)
            return jsonify({"id": pid}), 201

    @bp.post("/api/queue/<int:pid>/admit")
    def admit(pid):
        data = request.get_json(silent=True) or {}
        with session() as con:
            try:
                return jsonify(services.admit(con, pid, data.get("bed_id")))
            except ValueError as e:
                return error(str(e), 409)

    @bp.post("/api/queue/<int:pid>/urgency")
    def set_urgency(pid):
        u = int((request.get_json(silent=True) or {}).get("urgency", 1))
        if u not in (1, 2, 3):
            return error("urgency must be 1, 2 or 3")
        with session() as con:
            con.execute("UPDATE patients SET urgency = ? WHERE id = ? AND status = 'waiting'", (u, pid))
        return jsonify({"ok": True})

    @bp.post("/api/queue/<int:pid>/remove")
    def remove_from_queue(pid):
        with session() as con:
            con.execute("UPDATE patients SET status = 'removed' WHERE id = ? AND status = 'waiting'", (pid,))
        return jsonify({"ok": True})

    @bp.post("/api/settings")
    def update_settings():
        data = request.get_json(silent=True) or {}
        with session() as con:
            for k in ("w_urgency", "w_wait", "w_prob", "wait_cap"):
                if k in data:
                    db.set_setting(con, k, float(data[k]))
            return jsonify(db.get_settings(con))

    @bp.post("/api/triage")
    def triage():
        values = request.get_json(silent=True) or {}
        if values.get("esi") in (None, ""):
            return error("ESI level is required")
        out = models.predict_admit(values)
        out["suggested_urgency"] = models.urgency_from_esi(values["esi"])
        return jsonify(out)

    @bp.get("/api/beds")
    def beds():
        with session() as con:
            return jsonify(services.beds(con))

    @bp.post("/api/beds/<int:bed_id>/discharge")
    def discharge(bed_id):
        with session() as con:
            try:
                services.discharge(con, bed_id)
            except ValueError as e:
                return error(str(e), 409)
        return jsonify({"ok": True})

    @bp.get("/api/wards")
    def wards():
        with session() as con:
            return jsonify(services.wards(con))

    @bp.get("/api/metrics")
    def metrics():
        return jsonify(models.all_metrics())

    if not live:
        @bp.post("/api/simulate/next-day")
        def next_day():
            with session() as con:
                return jsonify(services.next_day(con))

        @bp.post("/api/simulate/reset")
        def reset():
            with session() as con:
                seed(con)
            return jsonify({"ok": True})
        return bp

    # ---------- setup, only for your own hospital ----------

    @bp.get("/api/setup")
    def get_setup():
        with session() as con:
            s = db.get_settings(con)
            return jsonify({"hospital_name": s.get("hospital_name", ""),
                            "typical_daily_admissions": float(s.get("typical_daily_admissions") or 6),
                            "created_on": s.get("created_on"), "wards": services.wards(con)})

    @bp.post("/api/setup")
    def save_setup():
        data = request.get_json(silent=True) or {}
        with session() as con:
            if "hospital_name" in data:
                db.set_setting(con, "hospital_name", str(data["hospital_name"]).strip())
            if data.get("typical_daily_admissions") not in (None, ""):
                n = float(data["typical_daily_admissions"])
                if n < 0:
                    return error("admissions per day cannot be negative")
                db.set_setting(con, "typical_daily_admissions", n)
        return get_setup()

    @bp.post("/api/wards")
    def create_ward():
        data = request.get_json(silent=True) or {}
        with session() as con:
            try:
                services.add_ward(con, data.get("name"), int(data.get("beds") or 0), data.get("prefix") or None)
            except ValueError as e:
                return error(str(e))
        return get_setup(), 201

    @bp.post("/api/wards/<int:ward_id>/beds")
    def create_beds(ward_id):
        data = request.get_json(silent=True) or {}
        with session() as con:
            try:
                services.add_beds(con, ward_id, int(data.get("count") or 0), data.get("prefix") or None)
            except ValueError as e:
                return error(str(e))
        return get_setup()

    @bp.delete("/api/wards/<int:ward_id>")
    def remove_ward(ward_id):
        with session() as con:
            try:
                services.delete_ward(con, ward_id)
            except ValueError as e:
                return error(str(e), 409)
        return get_setup()

    @bp.delete("/api/beds/<int:bed_id>")
    def remove_bed(bed_id):
        with session() as con:
            try:
                services.delete_bed(con, bed_id)
            except ValueError as e:
                return error(str(e), 409)
        return jsonify({"ok": True})

    @bp.post("/api/beds/<int:bed_id>/occupant")
    def add_occupant(bed_id):
        data = request.get_json(silent=True) or {}
        if not data.get("name"):
            return error("name is required")
        with session() as con:
            try:
                return jsonify(services.register_inpatient(con, bed_id, data)), 201
            except ValueError as e:
                return error(str(e), 409)

    @bp.post("/api/setup/wipe")
    def wipe():
        if (request.get_json(silent=True) or {}).get("confirm") != "DELETE":
            return error('send {"confirm": "DELETE"} to erase this hospital')
        with session() as con:
            for t in ("beds", "patients", "wards", "daily_log", "settings"):
                con.execute(f"DROP TABLE IF EXISTS {t}")
            init_live(con)
        return jsonify({"ok": True})

    return bp


app = Flask(__name__)
app.json.sort_keys = False
app.register_blueprint(platform("demo", "", db.DB_PATH, live=False))
app.register_blueprint(platform("hospital", "/hospital", db.LIVE_DB_PATH, live=True))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
