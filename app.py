import os
import sqlite3
import calendar
import uuid
import hmac
import hashlib
import requests

from datetime import datetime, date, timedelta
from flask import Flask, render_template, request, redirect, url_for, flash, session, g, abort

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-this-secret-key")

DB_PATH = "booking.db"
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "1234")

OPEN_HOUR = 9
CLOSE_HOUR = 21
MAX_HOURS_PER_BOOKING = 4
ADVANCE_BOOKING_DAYS = 30
CANCEL_DEADLINE_DAYS = 1
WEEKLY_CLOSED_WEEKDAY = 0  # 월요일

STATUS_KR = {
    "PENDING": "승인대기",
    "APPROVED": "승인완료",
    "REJECTED": "반려",
    "CANCELLED": "취소"
}


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def parse_int(v, default=None):
    try:
        return int(v)
    except:
        return default


def is_valid_phone(phone: str) -> bool:
    num = phone.replace("-", "").strip()
    return num.isdigit() and len(num) in (10, 11)


def hour_range_label(start_hour: int, end_hour: int) -> str:
    return f"{start_hour:02d}:00 ~ {end_hour:02d}:00"

def is_expired_reservation(reserve_date_str: str, end_hour: int) -> bool:
    try:
        reserve_dt = datetime.strptime(reserve_date_str, "%Y-%m-%d").date()
    except:
        return False

    now = datetime.now()
    today = now.date()
    current_hour = now.hour

    if reserve_dt < today:
        return True

    if reserve_dt == today and end_hour <= current_hour:
        return True

    return False

def require_admin():
    return session.get("admin")


def send_sms_solapi(to, text):
    api_key = os.getenv("SOLAPI_API_KEY", "").strip()
    api_secret = os.getenv("SOLAPI_API_SECRET", "").strip()
    sender = os.getenv("SOLAPI_SENDER", "").strip()

    if not api_key or not api_secret or not sender:
        print("SOLAPI 환경변수 없음. 문자 발송 생략")
        return False

    date_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    salt = uuid.uuid4().hex

    signature = hmac.new(
        api_secret.encode("utf-8"),
        f"{date_str}{salt}".encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

    auth = f'HMAC-SHA256 apiKey={api_key}, date={date_str}, salt={salt}, signature={signature}'

    url = "https://api.solapi.com/messages/v4/send"
    headers = {
        "Authorization": auth,
        "Content-Type": "application/json"
    }
    payload = {
        "message": {
            "to": to.replace("-", ""),
            "from": sender,
            "text": text
        }
    }

    try:
        res = requests.post(url, json=payload, headers=headers, timeout=10)
        print("SOLAPI:", res.status_code, res.text)
        return 200 <= res.status_code < 300
    except Exception as e:
        print("문자 발송 실패:", e)
        return False


def init_db():
    db = sqlite3.connect(DB_PATH)
    cur = db.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS facilities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        category TEXT NOT NULL,
        location TEXT NOT NULL,
        capacity INTEGER NOT NULL,
        description TEXT,
        equipment TEXT,
        notice TEXT,
        image TEXT DEFAULT '',
        is_active INTEGER NOT NULL DEFAULT 1
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS reservations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        facility_id INTEGER NOT NULL,
        reserve_date TEXT NOT NULL,
        start_hour INTEGER NOT NULL,
        end_hour INTEGER NOT NULL,
        name TEXT NOT NULL,
        phone TEXT NOT NULL,
        org_name TEXT,
        purpose TEXT NOT NULL,
        people_count INTEGER NOT NULL,
        agree_privacy INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'PENDING',
        admin_memo TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        FOREIGN KEY (facility_id) REFERENCES facilities(id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS closures (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        close_date TEXT NOT NULL UNIQUE,
        reason TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS notices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        category TEXT NOT NULL DEFAULT '공지사항',
        is_pinned INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("SELECT COUNT(*) FROM facilities")
    if cur.fetchone()[0] == 0:
        facilities = [
            (
                "다목적강당",
                "공연/행사",
                "본관 1층",
                120,
                "발표회, 진로특강, 청소년 공연, 지역행사 운영에 적합한 대형 공간입니다.",
                "음향장비, 무선마이크, 빔프로젝터, 스크린, 이동식 의자",
                "행사 준비 및 철수 시간을 포함하여 신청해 주세요.",
                "https://images.unsplash.com/photo-1511578314322-379afb476865?q=80&w=1200&auto=format&fit=crop"
            ),
            (
                "큰열림마당",
                "회의/교육",
                "본관 2층",
                40,
                "동아리 활동, 회의, 소규모 교육, 청소년 프로그램 진행에 적합한 중형 공간입니다.",
                "화이트보드, TV 모니터, 냉난방, 테이블",
                "정숙한 이용이 필요한 프로그램에 우선 배정될 수 있습니다.",
                "https://images.unsplash.com/photo-1497366754035-f200968a6e72?q=80&w=1200&auto=format&fit=crop"
            ),
            (
                "작은열림마당",
                "회의/상담",
                "본관 2층",
                20,
                "상담, 멘토링, 스터디, 소규모 회의에 적합한 공간입니다.",
                "화이트보드, 냉난방, 소형 모니터",
                "음식물 반입은 제한됩니다.",
                "https://images.unsplash.com/photo-1497366412874-3415097a27e7?q=80&w=1200&auto=format&fit=crop"
            ),
            (
                "창의활동실",
                "체험/활동",
                "별관 1층",
                30,
                "체험활동, 만들기 수업, 청소년 그룹 활동에 적합한 공간입니다.",
                "작업 테이블, 블루투스 스피커, 냉난방, 보관함",
                "바닥 오염 가능 활동은 사전 협의가 필요합니다.",
                "https://images.unsplash.com/photo-1517048676732-d65bc937f952?q=80&w=1200&auto=format&fit=crop"
            )
        ]
        cur.executemany("""
            INSERT INTO facilities
            (name, category, location, capacity, description, equipment, notice, image)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, facilities)

    cur.execute("SELECT COUNT(*) FROM notices")
    if cur.fetchone()[0] == 0:
        notices = [
            ("2026년 태안군청소년수련관 시설이용 안내", "운영시간, 휴관일, 예약 가능 기간, 취소 규정 등을 안내드립니다.", "공지사항", 1, "2026-04-01"),
            ("시설대관 신청 시 유의사항", "대관 신청 전 시설 정원, 이용 목적, 승인 절차를 반드시 확인해 주세요.", "시설안내", 0, "2026-03-28"),
            ("청소년 해양체험 프로그램 참가자 모집", "태안 지역 특성을 반영한 해양·환경 체험 프로그램 참가자를 모집합니다.", "프로그램", 0, "2026-03-22"),
            ("봄학기 동아리 활동 지원 안내", "동아리 활동 공간 사용 및 지원 절차를 안내합니다.", "공지사항", 0, "2026-03-18")
        ]
        cur.executemany("""
            INSERT INTO notices (title, content, category, is_pinned, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, notices)

    db.commit()
    db.close()


def get_facilities(active_only=True):
    db = get_db()
    sql = "SELECT * FROM facilities"
    if active_only:
        sql += " WHERE is_active = 1"
    sql += " ORDER BY id"
    return db.execute(sql).fetchall()


def get_latest_notices(limit=5):
    db = get_db()
    return db.execute("""
        SELECT * FROM notices
        ORDER BY is_pinned DESC, created_at DESC, id DESC
        LIMIT ?
    """, (limit,)).fetchall()


def get_all_notices():
    db = get_db()
    return db.execute("""
        SELECT * FROM notices
        ORDER BY is_pinned DESC, created_at DESC, id DESC
    """).fetchall()


def get_notice(nid):
    db = get_db()
    return db.execute("SELECT * FROM notices WHERE id = ?", (nid,)).fetchone()


def get_closure_map():
    db = get_db()
    rows = db.execute("SELECT close_date, reason FROM closures").fetchall()
    return {r["close_date"]: r["reason"] for r in rows}


def is_closed_date(target_date):
    if target_date.weekday() == WEEKLY_CLOSED_WEEKDAY:
        return True, "정기휴관일(매주 월요일)"
    closure_map = get_closure_map()
    reason = closure_map.get(target_date.isoformat())
    if reason:
        return True, reason
    return False, ""


def get_booking_map(selected_date):
    db = get_db()
    rows = db.execute("""
        SELECT facility_id, start_hour, end_hour, status
        FROM reservations
        WHERE reserve_date = ?
          AND status IN ('PENDING', 'APPROVED')
    """, (selected_date,)).fetchall()

    booked = {}
    for r in rows:
        for h in range(r["start_hour"], r["end_hour"]):
            booked[(r["facility_id"], h)] = r["status"]

    try:
        selected_dt = datetime.strptime(selected_date, "%Y-%m-%d").date()
    except ValueError:
        return booked

    now = datetime.now()
    today = now.date()
    current_hour = now.hour

    facilities = get_facilities()

    for f in facilities:
        for h in range(OPEN_HOUR, CLOSE_HOUR):
            if selected_dt < today:
                booked[(f["id"], h)] = "CLOSED"
            elif selected_dt == today and h <= current_hour:
                if (f["id"], h) not in booked:
                    booked[(f["id"], h)] = "CLOSED"

    return booked

def get_month_matrix(year, month):
    cal = calendar.Calendar(firstweekday=6)
    return cal.monthdatescalendar(year, month)


def get_month_status_map(year, month):
    db = get_db()
    start_date = date(year, month, 1)
    end_day = calendar.monthrange(year, month)[1]
    end_date = date(year, month, end_day)

    rows = db.execute("""
        SELECT reserve_date, status, COUNT(*) AS cnt
        FROM reservations
        WHERE reserve_date BETWEEN ? AND ?
          AND status IN ('PENDING', 'APPROVED')
        GROUP BY reserve_date, status
    """, (start_date.isoformat(), end_date.isoformat())).fetchall()

    result = {}
    for r in rows:
        if r["reserve_date"] not in result:
            result[r["reserve_date"]] = {"PENDING": 0, "APPROVED": 0}
        result[r["reserve_date"]][r["status"]] = r["cnt"]
    return result


def get_summary_counts():
    db = get_db()
    rows = db.execute("""
        SELECT status, COUNT(*) AS cnt
        FROM reservations
        GROUP BY status
    """).fetchall()

    counts = {"PENDING": 0, "APPROVED": 0, "REJECTED": 0, "CANCELLED": 0}
    for r in rows:
        counts[r["status"]] = r["cnt"]
    return counts


@app.context_processor
def inject_globals():
    today = date.today()
    max_date = today + timedelta(days=ADVANCE_BOOKING_DAYS)
    return {
        "STATUS_KR": STATUS_KR,
        "OPEN_HOUR": OPEN_HOUR,
        "CLOSE_HOUR": CLOSE_HOUR,
        "today": today.isoformat(),
        "today_obj": today,
        "max_date": max_date.isoformat(),
        "MAX_HOURS_PER_BOOKING": MAX_HOURS_PER_BOOKING,
        "ADVANCE_BOOKING_DAYS": ADVANCE_BOOKING_DAYS,
        "CANCEL_DEADLINE_DAYS": CANCEL_DEADLINE_DAYS,
        "WEEKLY_CLOSED_WEEKDAY": WEEKLY_CLOSED_WEEKDAY,
        "is_expired_reservation": is_expired_reservation
    }


@app.route("/")
def home():
    notices = get_latest_notices(5)
    facilities = get_facilities()[:4]
    banners = [
        {
            "title": "태안군청소년수련관 시설예약 안내",
            "desc": "다목적강당, 큰열림마당, 작은열림마당, 창의활동실을 온라인으로 예약하세요.",
            "img": "https://images.unsplash.com/photo-1507525428034-b723cf961d3e?q=80&w=1600&auto=format&fit=crop",
            "link": url_for("reserve_page"),
            "button": "시설 예약하기"
        },
        {
            "title": "월간 운영현황과 휴관일 확인",
            "desc": "예약 가능일, 확정 일정, 정기휴관일과 추가 휴관일을 미리 확인할 수 있습니다.",
            "img": "https://images.unsplash.com/photo-1495364141860-b0d03eccd065?q=80&w=1600&auto=format&fit=crop",
            "link": url_for("calendar_view"),
            "button": "월간 현황 보기"
        },
        {
            "title": "청소년 프로그램과 공지사항",
            "desc": "프로그램 모집, 시설 이용안내, 중요 공지를 빠르게 확인해 보세요.",
            "img": "https://images.unsplash.com/photo-1517486808906-6ca8b3f04846?q=80&w=1600&auto=format&fit=crop",
            "link": url_for("notices"),
            "button": "공지사항 보기"
        }
    ]
    return render_template("home.html", notices=notices, facilities=facilities, banners=banners)


@app.route("/reserve")
def reserve_page():
    selected_date = request.args.get("date", date.today().isoformat())
    try:
        now = datetime.now()
        today = now.date()
        current_hour = now.hour
        selected_dt = datetime.strptime(selected_date, "%Y-%m-%d").date()
    except ValueError:
        selected_dt = date.today()
        selected_date = selected_dt.isoformat()

    facilities = get_facilities()
    booking_map = get_booking_map(selected_date)
    closed, closed_reason = is_closed_date(selected_dt)

    return render_template(
        "index.html",
        facilities=facilities,
        selected_date=selected_date,
        booking_map=booking_map,
        closed=closed,
        closed_reason=closed_reason
    )


@app.route("/calendar")
def calendar_view():
    today = date.today()
    year = parse_int(request.args.get("year"), today.year)
    month = parse_int(request.args.get("month"), today.month)

    if month < 1 or month > 12:
        month = today.month

    month_matrix = get_month_matrix(year, month)
    status_map = get_month_status_map(year, month)
    closure_map = get_closure_map()

    prev_month = month - 1
    prev_year = year
    if prev_month == 0:
        prev_month = 12
        prev_year -= 1

    next_month = month + 1
    next_year = year
    if next_month == 13:
        next_month = 1
        next_year += 1

    return render_template(
        "calendar.html",
        year=year,
        month=month,
        month_matrix=month_matrix,
        status_map=status_map,
        closure_map=closure_map,
        prev_year=prev_year,
        prev_month=prev_month,
        next_year=next_year,
        next_month=next_month
    )


@app.route("/notices")
def notices():
    rows = get_all_notices()
    return render_template("notices.html", rows=rows)


@app.route("/notices/<int:nid>")
def notice_detail(nid):
    row = get_notice(nid)
    if not row:
        abort(404)
    return render_template("notice_detail.html", row=row)


@app.route("/reserve/submit", methods=["POST"])
def reserve():
    facility_id = parse_int(request.form.get("facility_id"))
    reserve_date = request.form.get("reserve_date", "").strip()
    start_hour = parse_int(request.form.get("start_hour"))
    end_hour = parse_int(request.form.get("end_hour"))
    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()
    org_name = request.form.get("org_name", "").strip()
    purpose = request.form.get("purpose", "").strip()
    people_count = parse_int(request.form.get("people_count"))
    agree_privacy = 1 if request.form.get("agree_privacy") == "on" else 0

    if not all([facility_id, reserve_date, start_hour, end_hour, name, phone, purpose, people_count]):
        flash("필수 입력값이 누락되었습니다.", "danger")
        return redirect(url_for("reserve_page", date=reserve_date or date.today().isoformat()))

    if not agree_privacy:
        flash("개인정보 수집 및 이용에 동의해야 예약할 수 있습니다.", "danger")
        return redirect(url_for("reserve_page", date=reserve_date))

    if not is_valid_phone(phone):
        flash("휴대폰 번호 형식이 올바르지 않습니다.", "danger")
        return redirect(url_for("reserve_page", date=reserve_date))

    try:
        reserve_dt = datetime.strptime(reserve_date, "%Y-%m-%d").date()
    except ValueError:
        flash("날짜 형식이 올바르지 않습니다.", "danger")
        return redirect(url_for("reserve_page"))

    today = date.today()
    if reserve_dt < today:
        flash("지난 날짜는 예약할 수 없습니다.", "danger")
        return redirect(url_for("reserve_page", date=reserve_date))

    if reserve_dt > today + timedelta(days=ADVANCE_BOOKING_DAYS):
        flash(f"예약은 오늘 기준 {ADVANCE_BOOKING_DAYS}일 이내까지만 가능합니다.", "danger")
        return redirect(url_for("reserve_page", date=reserve_date))

    closed, closed_reason = is_closed_date(reserve_dt)
    if closed:
        flash(f"해당 날짜는 휴관일입니다. ({closed_reason})", "danger")
        return redirect(url_for("reserve_page", date=reserve_date))

    if start_hour < OPEN_HOUR or end_hour > CLOSE_HOUR or start_hour >= end_hour:
        flash("예약 시간이 올바르지 않습니다.", "danger")
        return redirect(url_for("reserve_page", date=reserve_date))

    if end_hour - start_hour > MAX_HOURS_PER_BOOKING:
        flash(f"1회 예약은 최대 {MAX_HOURS_PER_BOOKING}시간까지만 가능합니다.", "danger")
        return redirect(url_for("reserve_page", date=reserve_date))

    db = get_db()
    facility = db.execute("""
        SELECT * FROM facilities
        WHERE id = ? AND is_active = 1
    """, (facility_id,)).fetchone()

    if not facility:
        flash("시설 정보를 찾을 수 없습니다.", "danger")
        return redirect(url_for("reserve_page", date=reserve_date))

    if people_count <= 0 or people_count > facility["capacity"]:
        flash(f"예약 인원은 1명 이상, 정원 {facility['capacity']}명 이하로 입력하세요.", "danger")
        return redirect(url_for("reserve_page", date=reserve_date))

    conflict = db.execute("""
        SELECT id
        FROM reservations
        WHERE facility_id = ?
          AND reserve_date = ?
          AND status IN ('PENDING', 'APPROVED')
          AND NOT (end_hour <= ? OR start_hour >= ?)
    """, (facility_id, reserve_date, start_hour, end_hour)).fetchone()

    if conflict:
        flash("선택한 시간 구간에 이미 예약이 있습니다.", "danger")
        return redirect(url_for("reserve_page", date=reserve_date))

    db.execute("""
        INSERT INTO reservations (
            facility_id, reserve_date, start_hour, end_hour,
            name, phone, org_name, purpose, people_count,
            agree_privacy, status, admin_memo, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', '', ?)
    """, (
        facility_id, reserve_date, start_hour, end_hour,
        name, phone, org_name, purpose, people_count,
        agree_privacy, datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ))
    db.commit()

    flash("예약 신청이 완료되었습니다. 관리자 승인 후 확정됩니다.", "success")
    return redirect(url_for("my_reservations"))


@app.route("/my", methods=["GET", "POST"])
def my_reservations():
    reservations = None
    name = ""
    phone = ""

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()

        db = get_db()
        reservations = db.execute("""
            SELECT r.*, f.name AS facility_name, f.location, f.category
            FROM reservations r
            JOIN facilities f ON r.facility_id = f.id
            WHERE r.name = ? AND r.phone = ?
            ORDER BY r.reserve_date DESC, r.start_hour ASC
        """, (name, phone)).fetchall()

        if not reservations:
            flash("조회된 예약이 없습니다.", "warning")

    return render_template(
        "my.html",
        reservations=reservations,
        name=name,
        phone=phone,
        hour_range_label=hour_range_label
    )


@app.route("/cancel/<int:reservation_id>", methods=["POST"])
def cancel_reservation(reservation_id):
    name = request.form.get("name", "").strip()
    phone = request.form.get("phone", "").strip()

    db = get_db()
    row = db.execute("""
        SELECT *
        FROM reservations
        WHERE id = ? AND name = ? AND phone = ?
    """, (reservation_id, name, phone)).fetchone()

    if not row:
        flash("예약 정보를 찾을 수 없습니다.", "danger")
        return redirect(url_for("my_reservations"))

    if row["status"] == "CANCELLED":
        flash("이미 취소된 예약입니다.", "warning")
        return redirect(url_for("my_reservations"))

    reserve_dt = datetime.strptime(row["reserve_date"], "%Y-%m-%d").date()
    if reserve_dt <= date.today() + timedelta(days=CANCEL_DEADLINE_DAYS - 1):
        flash("온라인 취소 가능 기한이 지났습니다. 관리자에게 문의하세요.", "danger")
        return redirect(url_for("my_reservations"))

    db.execute("UPDATE reservations SET status = 'CANCELLED' WHERE id = ?", (reservation_id,))
    db.commit()

    flash("예약이 취소되었습니다.", "success")
    return redirect(url_for("my_reservations"))


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        pw = request.form.get("password", "").strip()
        if pw == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("관리자 비밀번호가 올바르지 않습니다.", "danger")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/admin")
def admin_dashboard():
    if not require_admin():
        return redirect(url_for("admin_login"))

    q_date = request.args.get("date", "").strip()
    q_status = request.args.get("status", "").strip()
    q_facility = request.args.get("facility_id", "").strip()

    sql = """
        SELECT r.*, f.name AS facility_name, f.location, f.category
        FROM reservations r
        JOIN facilities f ON r.facility_id = f.id
        WHERE 1=1
    """
    params = []

    if q_date:
        sql += " AND r.reserve_date = ?"
        params.append(q_date)
    if q_status:
        sql += " AND r.status = ?"
        params.append(q_status)
    if q_facility:
        sql += " AND r.facility_id = ?"
        params.append(q_facility)

    sql += """
        ORDER BY
            CASE r.status
                WHEN 'PENDING' THEN 1
                WHEN 'APPROVED' THEN 2
                WHEN 'REJECTED' THEN 3
                WHEN 'CANCELLED' THEN 4
                ELSE 5
            END,
            r.reserve_date ASC,
            r.start_hour ASC
    """

    db = get_db()
    rows = db.execute(sql, params).fetchall()
    counts = get_summary_counts()
    facilities = get_facilities(active_only=False)

    return render_template(
        "admin_dashboard.html",
        rows=rows,
        counts=counts,
        q_date=q_date,
        q_status=q_status,
        q_facility=q_facility,
        facilities=facilities,
        hour_range_label=hour_range_label
    )


@app.route("/admin/update/<int:reservation_id>", methods=["POST"])
def admin_update_reservation(reservation_id):
    if not require_admin():
        return redirect(url_for("admin_login"))

    new_status = request.form.get("status", "").strip()
    admin_memo = request.form.get("admin_memo", "").strip()

    if new_status not in ["PENDING", "APPROVED", "REJECTED", "CANCELLED"]:
        flash("잘못된 상태값입니다.", "danger")
        return redirect(url_for("admin_dashboard"))

    db = get_db()
    row = db.execute("""
        SELECT r.*, f.name AS facility_name
        FROM reservations r
        JOIN facilities f ON r.facility_id = f.id
        WHERE r.id = ?
    """, (reservation_id,)).fetchone()

    if not row:
        flash("예약 정보를 찾을 수 없습니다.", "danger")
        return redirect(url_for("admin_dashboard"))

    old_status = row["status"]

    db.execute("""
        UPDATE reservations
        SET status = ?, admin_memo = ?
        WHERE id = ?
    """, (new_status, admin_memo, reservation_id))
    db.commit()

    if old_status != new_status:
        msg = None
        if new_status == "APPROVED":
            msg = f"[태안군청소년수련관] 예약이 승인되었습니다. {row['reserve_date']} {row['start_hour']:02d}:00~{row['end_hour']:02d}:00 / {row['facility_name']}"
        elif new_status == "REJECTED":
            msg = f"[태안군청소년수련관] 예약이 반려되었습니다. {row['reserve_date']} / {row['facility_name']}"
        elif new_status == "CANCELLED":
            msg = f"[태안군청소년수련관] 예약이 취소 처리되었습니다. {row['reserve_date']} / {row['facility_name']}"

        if msg:
            send_sms_solapi(row["phone"], msg)

    flash("예약 상태가 변경되었습니다.", "success")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/delete-selected", methods=["POST"])
def admin_delete_selected_reservations():
    if not require_admin():
        return redirect(url_for("admin_login"))

    ids = request.form.getlist("reservation_ids")

    if not ids:
        flash("선택된 만료 예약이 없습니다.", "warning")
        return redirect(url_for("admin_dashboard"))

    safe_ids = []
    for v in ids:
        try:
            safe_ids.append(int(v))
        except:
            pass

    if not safe_ids:
        flash("삭제할 예약값이 올바르지 않습니다.", "danger")
        return redirect(url_for("admin_dashboard"))

    db = get_db()
    placeholders = ",".join(["?"] * len(safe_ids))

    rows = db.execute(f"""
        SELECT id, reserve_date, end_hour
        FROM reservations
        WHERE id IN ({placeholders})
    """, safe_ids).fetchall()

    deletable_ids = [
        r["id"] for r in rows
        if is_expired_reservation(r["reserve_date"], r["end_hour"])
    ]

    if not deletable_ids:
        flash("삭제 가능한 만료 예약이 없습니다.", "warning")
        return redirect(url_for("admin_dashboard"))

    delete_placeholders = ",".join(["?"] * len(deletable_ids))
    cur = db.execute(f"""
        DELETE FROM reservations
        WHERE id IN ({delete_placeholders})
    """, deletable_ids)
    db.commit()

    flash(f"만료된 예약 {cur.rowcount}건을 삭제했습니다.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/delete-expired-all", methods=["POST"])
def admin_delete_all_expired_reservations():
    if not require_admin():
        return redirect(url_for("admin_login"))

    db = get_db()
    rows = db.execute("""
        SELECT id, reserve_date, end_hour
        FROM reservations
    """).fetchall()

    deletable_ids = [
        r["id"] for r in rows
        if is_expired_reservation(r["reserve_date"], r["end_hour"])
    ]

    if not deletable_ids:
        flash("삭제할 만료 예약이 없습니다.", "warning")
        return redirect(url_for("admin_dashboard"))

    placeholders = ",".join(["?"] * len(deletable_ids))
    cur = db.execute(f"""
        DELETE FROM reservations
        WHERE id IN ({placeholders})
    """, deletable_ids)
    db.commit()

    flash(f"만료된 예약 {cur.rowcount}건을 전체 삭제했습니다.", "success")
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/facilities", methods=["GET", "POST"])
def admin_facilities():
    if not require_admin():
        return redirect(url_for("admin_login"))

    db = get_db()

    if request.method == "POST":
        action = request.form.get("action", "").strip()

        if action == "create":
            name = request.form.get("name", "").strip()
            category = request.form.get("category", "").strip()
            location = request.form.get("location", "").strip()
            capacity = parse_int(request.form.get("capacity"))
            description = request.form.get("description", "").strip()
            equipment = request.form.get("equipment", "").strip()
            notice = request.form.get("notice", "").strip()
            image = request.form.get("image", "").strip()

            if not all([name, category, location, capacity]):
                flash("시설 등록 필수값이 누락되었습니다.", "danger")
                return redirect(url_for("admin_facilities"))

            db.execute("""
                INSERT INTO facilities
                (name, category, location, capacity, description, equipment, notice, image, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            """, (name, category, location, capacity, description, equipment, notice, image))
            db.commit()
            flash("시설이 등록되었습니다.", "success")
            return redirect(url_for("admin_facilities"))

        if action == "update":
            fid = parse_int(request.form.get("facility_id"))
            name = request.form.get("name", "").strip()
            category = request.form.get("category", "").strip()
            location = request.form.get("location", "").strip()
            capacity = parse_int(request.form.get("capacity"))
            description = request.form.get("description", "").strip()
            equipment = request.form.get("equipment", "").strip()
            notice = request.form.get("notice", "").strip()
            image = request.form.get("image", "").strip()
            is_active = 1 if request.form.get("is_active") == "on" else 0

            if not all([fid, name, category, location, capacity]):
                flash("시설 수정 필수값이 누락되었습니다.", "danger")
                return redirect(url_for("admin_facilities"))

            db.execute("""
                UPDATE facilities
                SET name=?, category=?, location=?, capacity=?,
                    description=?, equipment=?, notice=?, image=?, is_active=?
                WHERE id=?
            """, (name, category, location, capacity, description, equipment, notice, image, is_active, fid))
            db.commit()
            flash("시설 정보가 수정되었습니다.", "success")
            return redirect(url_for("admin_facilities"))

    facilities = get_facilities(active_only=False)
    return render_template("admin_facilities.html", facilities=facilities)


@app.route("/admin/closures", methods=["GET", "POST"])
def admin_closures():
    if not require_admin():
        return redirect(url_for("admin_login"))

    db = get_db()

    if request.method == "POST":
        action = request.form.get("action", "").strip()

        if action == "create":
            close_date = request.form.get("close_date", "").strip()
            reason = request.form.get("reason", "").strip()

            if not all([close_date, reason]):
                flash("휴관일 등록값이 부족합니다.", "danger")
                return redirect(url_for("admin_closures"))

            try:
                target_dt = datetime.strptime(close_date, "%Y-%m-%d").date()
            except ValueError:
                flash("휴관일 날짜 형식이 올바르지 않습니다.", "danger")
                return redirect(url_for("admin_closures"))

            if target_dt.weekday() == WEEKLY_CLOSED_WEEKDAY:
                flash("월요일은 이미 정기휴관일입니다.", "warning")
                return redirect(url_for("admin_closures"))

            try:
                db.execute("INSERT INTO closures (close_date, reason) VALUES (?, ?)", (close_date, reason))
                db.commit()
                flash("휴관일이 등록되었습니다.", "success")
            except sqlite3.IntegrityError:
                flash("이미 등록된 휴관일입니다.", "warning")

            return redirect(url_for("admin_closures"))

        if action == "delete":
            cid = parse_int(request.form.get("closure_id"))
            db.execute("DELETE FROM closures WHERE id = ?", (cid,))
            db.commit()
            flash("휴관일이 삭제되었습니다.", "success")
            return redirect(url_for("admin_closures"))

    rows = db.execute("SELECT * FROM closures ORDER BY close_date ASC").fetchall()
    return render_template("admin_closures.html", rows=rows)


@app.route("/admin/notices", methods=["GET", "POST"])
def admin_notices():
    if not require_admin():
        return redirect(url_for("admin_login"))

    db = get_db()

    if request.method == "POST":
        action = request.form.get("action", "").strip()

        if action == "create":
            title = request.form.get("title", "").strip()
            category = request.form.get("category", "").strip() or "공지사항"
            content = request.form.get("content", "").strip()
            is_pinned = 1 if request.form.get("is_pinned") == "on" else 0

            if not title or not content:
                flash("공지 제목과 내용은 필수입니다.", "danger")
                return redirect(url_for("admin_notices"))

            db.execute("""
                INSERT INTO notices (title, content, category, is_pinned, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (title, content, category, is_pinned, date.today().isoformat()))
            db.commit()
            flash("공지가 등록되었습니다.", "success")
            return redirect(url_for("admin_notices"))

        if action == "update":
            nid = parse_int(request.form.get("notice_id"))
            title = request.form.get("title", "").strip()
            category = request.form.get("category", "").strip() or "공지사항"
            content = request.form.get("content", "").strip()
            is_pinned = 1 if request.form.get("is_pinned") == "on" else 0

            if not nid or not title or not content:
                flash("공지 수정값이 올바르지 않습니다.", "danger")
                return redirect(url_for("admin_notices"))

            db.execute("""
                UPDATE notices
                SET title = ?, category = ?, content = ?, is_pinned = ?
                WHERE id = ?
            """, (title, category, content, is_pinned, nid))
            db.commit()
            flash("공지가 수정되었습니다.", "success")
            return redirect(url_for("admin_notices"))

        if action == "delete":
            nid = parse_int(request.form.get("notice_id"))
            db.execute("DELETE FROM notices WHERE id = ?", (nid,))
            db.commit()
            flash("공지가 삭제되었습니다.", "success")
            return redirect(url_for("admin_notices"))

    notices = get_all_notices()
    return render_template("admin_notices.html", notices=notices)

# ==== 수련관 소개 ====
@app.route("/intro/greeting")
def intro_greeting():
    return render_template("intro_greeting.html")

@app.route("/intro/guide")
def intro_guide():
    return render_template("intro_guide.html")

@app.route("/intro/hours")
def intro_hours():
    return render_template("intro_hours.html")

@app.route("/intro/location")
def intro_location():
    return render_template("intro_location.html")

# ==== 청소년활동 ====

@app.route("/programs/current")
def programs_current():
    return render_template("programs_current.html")

@app.route("/programs/open")
def programs_open():
    return render_template("programs_open.html")

@app.route("/activities/gallery")
def activity_gallery():
    return render_template("activity_gallery.html")

@app.route("/schedule/monthly")
def monthly_schedule():
    return render_template("monthly_schedule.html")

#==== 프로그램 신청 ====

@app.route("/program/apply")
def program_apply():
    return render_template("program_apply.html")

@app.route("/program/apply/lookup")
def program_apply_lookup():
    return render_template("program_apply_lookup.html")

@app.route("/program/apply/guide")
def program_apply_guide():
    return render_template("program_apply_guide.html")

#==== 시설안내 ====

@app.route("/facility/info")
def facility_info():
    return render_template("facility_info.html")

@app.route("/facility/guide")
def facility_guide():
    return render_template("facility_guide.html")

#=== 알림마당 ====
@app.route("/free-board")
def free_board():
    return render_template("free_board.html")

@app.route("/faq")
def faq():
    return render_template("faq.html")

@app.route("/contact")
def contact():
    return render_template("contact.html")

@app.route("/newsletter")
def newsletter():
    return render_template("newsletter.html")

if __name__ == "__main__":
    init_db()
    print("서버 시작됨")
    app.run(host="0.0.0.0", port=5001, debug=True)