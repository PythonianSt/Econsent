# streamlit_app.py
# Thai E-Consent App
# Streamlit + PostgreSQL + Signatures + PDF + Admin Dashboard
#
# requirements.txt:
# streamlit
# pandas
# psycopg2-binary
# reportlab
# pytz
# pillow
# streamlit-drawable-canvas
#
# Streamlit Cloud secrets.toml example:
# DATABASE_URL = "postgresql://USER:PASSWORD@HOST:5432/DBNAME?sslmode=require"
# Optional, if you upload a Thai font file into your repo:
# THAI_FONT_PATH = "fonts/NotoSansThai-Regular.ttf"

import base64
import io
import os
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import psycopg2
import psycopg2.extras
import pytz
import streamlit as st
from PIL import Image
from streamlit_drawable_canvas import st_canvas

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image as RLImage

# ---------------- CONFIG ----------------
st.set_page_config(page_title="Thai E-Consent", layout="wide")
BKK = pytz.timezone("Asia/Bangkok")

TABLE_NAME = "consent_records"

# ---------------- DATABASE ----------------
def get_database_url() -> str:
    """Read PostgreSQL URL from Streamlit secrets or environment variable."""
    db_url = st.secrets.get("DATABASE_URL", None) or os.getenv("DATABASE_URL")
    if not db_url:
        st.error(
            "Missing DATABASE_URL. Add it to Streamlit secrets, e.g.\n\n"
            'DATABASE_URL = "postgresql://USER:PASSWORD@HOST:5432/DBNAME?sslmode=require"'
        )
        st.stop()
    return db_url

@st.cache_resource
def get_conn():
    return psycopg2.connect(
        st.secrets["DATABASE_URL"]
    )

conn = get_conn()


def init_db():
    """Create table if it does not already exist."""
    sql = f"""
    CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
        id BIGSERIAL PRIMARY KEY,
        patient_id TEXT NOT NULL,
        patient_name TEXT NOT NULL,
        age INTEGER NOT NULL CHECK (age BETWEEN 1 AND 120),
        doctor_name TEXT,
        nurse_name TEXT,
        procedure TEXT NOT NULL,
        agree BOOLEAN NOT NULL DEFAULT FALSE,
        timestamp_bkk TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        patient_signature TEXT,
        doctor_signature TEXT,
        nurse_signature TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_consent_records_created_at
        ON {TABLE_NAME} (created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_consent_records_patient_id
        ON {TABLE_NAME} (patient_id);
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()


def insert_record(record: dict):
    sql = f"""
    INSERT INTO {TABLE_NAME} (
        patient_id, patient_name, age, doctor_name, nurse_name, procedure, agree,
        timestamp_bkk, patient_signature, doctor_signature, nurse_signature
    )
    VALUES (
        %(patient_id)s, %(patient_name)s, %(age)s, %(doctor_name)s, %(nurse_name)s,
        %(procedure)s, %(agree)s, %(timestamp_bkk)s, %(patient_signature)s,
        %(doctor_signature)s, %(nurse_signature)s
    )
    RETURNING id;
    """
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, record)
            new_id = cur.fetchone()[0]
        conn.commit()
    return new_id


def fetch_records() -> pd.DataFrame:
    sql = f"""
    SELECT
        id, patient_id, patient_name, age, doctor_name, nurse_name, procedure,
        agree, timestamp_bkk, created_at,
        patient_signature, doctor_signature, nurse_signature
    FROM {TABLE_NAME}
    ORDER BY created_at DESC;
    """
    with get_conn() as conn:
        return pd.read_sql(sql, conn)


# Initialize DB once at app start.
try:
    init_db()
except Exception as e:
    st.error(f"PostgreSQL connection/table error: {e}")
    st.stop()

# ---------------- PDF FONT ----------------
def setup_pdf_font() -> str:
    """Register a Thai-capable font if available; otherwise fallback to Helvetica."""
    candidates = []

    secret_font = st.secrets.get("THAI_FONT_PATH", None)
    if secret_font:
        candidates.append(secret_font)

    candidates.extend([
        "fonts/NotoSansThai-Regular.ttf",
        "NotoSansThai-Regular.ttf",
        "THSarabunNew.ttf",
        "fonts/THSarabunNew.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ])

    for path in candidates:
        if path and Path(path).exists():
            pdfmetrics.registerFont(TTFont("ThaiFont", path))
            return "ThaiFont"

    return "Helvetica"

PDF_FONT = setup_pdf_font()

# ---------------- HELPERS ----------------
def now_bkk():
    return datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")


def sig_pad(label, key):
    st.markdown(f"### {label}")
    canvas = st_canvas(
        stroke_width=2,
        stroke_color="#000000",
        background_color="#FFFFFF",
        height=180,
        width=500,
        drawing_mode="freedraw",
        key=key,
    )

    if canvas.image_data is not None:
        img = Image.fromarray(canvas.image_data.astype("uint8"))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    return None


def decode_sig_to_tempfile(sig64):
    data = base64.b64decode(sig64)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    tmp.write(data)
    tmp.close()
    return tmp.name


def create_pdf(record):
    tmp_pdf = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    filename = tmp_pdf.name
    tmp_pdf.close()

    doc = SimpleDocTemplate(
        filename,
        pagesize=A4,
        rightMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        topMargin=1.5 * cm,
        bottomMargin=1.5 * cm,
    )

    styles = getSampleStyleSheet()
    normal = ParagraphStyle(
        "ThaiNormal",
        parent=styles["Normal"],
        fontName=PDF_FONT,
        fontSize=12,
        leading=18,
    )
    title = ParagraphStyle(
        "ThaiTitle",
        parent=styles["Title"],
        fontName=PDF_FONT,
        fontSize=18,
        leading=24,
    )

    story = [Paragraph("ใบยินยอมทำหัตถการ", title), Spacer(1, 12)]

    labels = {
        "record_id": "เลขที่บันทึก",
        "patient_id": "HN / เลขบัตร",
        "patient_name": "ชื่อผู้ป่วย",
        "age": "อายุ",
        "doctor_name": "แพทย์",
        "nurse_name": "พยาบาลพยาน",
        "procedure": "หัตถการ",
        "agree": "ยินยอม",
        "timestamp_bkk": "วันที่เวลา ประเทศไทย",
    }

    for key in [
        "record_id", "patient_id", "patient_name", "age", "doctor_name",
        "nurse_name", "procedure", "agree", "timestamp_bkk"
    ]:
        if key in record:
            story.append(Paragraph(f"{labels.get(key, key)}: {record.get(key)}", normal))
            story.append(Spacer(1, 6))

    sig_labels = {
        "patient_signature": "ลายเซ็นผู้ป่วย",
        "doctor_signature": "ลายเซ็นแพทย์",
        "nurse_signature": "ลายเซ็นพยาบาลพยาน",
    }

    for sig_key, sig_label in sig_labels.items():
        sig64 = record.get(sig_key)
        if sig64:
            imgfile = decode_sig_to_tempfile(sig64)
            story.append(Spacer(1, 8))
            story.append(Paragraph(sig_label, normal))
            story.append(RLImage(imgfile, width=7 * cm, height=2.8 * cm))

    doc.build(story)
    return filename


# ---------------- MENU ----------------
menu = st.sidebar.radio("Menu", ["Patient Consent Form", "Admin Dashboard"])

# ===================================================
# PATIENT PAGE
# ===================================================
if menu == "Patient Consent Form":
    st.title("📄 ระบบยินยอมทำหัตถการ")

    c1, c2 = st.columns(2)

    with c1:
        patient_id = st.text_input("HN / เลขบัตร")
        patient_name = st.text_input("ชื่อผู้ป่วย")
        age = st.number_input("อายุ", min_value=1, max_value=120, value=20)

    with c2:
        doctor_name = st.text_input("แพทย์")
        nurse_name = st.text_input("พยาบาลพยาน")
        procedure = st.selectbox("หัตถการ", ["Ankle Block", "Brachial Block"])

    st.markdown("---")

    if procedure == "Ankle Block":
        st.info(
            """
การฉีดยาชาบริเวณข้อเท้าเพื่อทำแผล

ความเสี่ยง:
- เจ็บ
- ชา
- เลือดออก
- ติดเชื้อ
- แพ้ยา
"""
        )
    else:
        st.info(
            """
การฉีดยาชาบริเวณแขนเพื่อทำแผลมือ

ความเสี่ยง:
- เจ็บ
- ชา/อ่อนแรงชั่วคราว
- เลือดออก
- แพ้ยา
- เส้นประสาทบาดเจ็บ พบน้อย
"""
        )

    agree = st.checkbox("ข้าพเจ้ายินยอม")

    st.markdown("---")
    patient_sig = sig_pad("ผู้ป่วยเซ็นชื่อ", "patient_signature_canvas")
    doctor_sig = sig_pad("แพทย์เซ็นชื่อ", "doctor_signature_canvas")
    nurse_sig = sig_pad("พยาบาลพยานเซ็นชื่อ", "nurse_signature_canvas")

    if st.button("💾 Save", type="primary"):
        if not patient_id.strip() or not patient_name.strip():
            st.warning("กรุณากรอก HN / เลขบัตร และชื่อผู้ป่วย")
            st.stop()
        if not agree:
            st.warning("กรุณาติ๊กยินยอมก่อนบันทึก")
            st.stop()
        if not patient_sig:
            st.warning("กรุณาให้ผู้ป่วยเซ็นชื่อ")
            st.stop()

        rec = {
            "patient_id": patient_id.strip(),
            "patient_name": patient_name.strip(),
            "age": int(age),
            "doctor_name": doctor_name.strip(),
            "nurse_name": nurse_name.strip(),
            "procedure": procedure,
            "agree": bool(agree),
            "timestamp_bkk": now_bkk(),
            "patient_signature": patient_sig,
            "doctor_signature": doctor_sig,
            "nurse_signature": nurse_sig,
        }

        try:
            new_id = insert_record(rec)
            rec["record_id"] = new_id
            st.success(f"Saved to PostgreSQL. Record ID: {new_id}")

            pdf = create_pdf(rec)
            with open(pdf, "rb") as f:
                st.download_button(
                    "📄 Download PDF",
                    f,
                    file_name=f"{patient_id.strip()}_consent.pdf",
                    mime="application/pdf",
                )
        except Exception as e:
            st.error(f"Save error: {e}")

# ===================================================
# ADMIN PAGE
# ===================================================
if menu == "Admin Dashboard":
    st.title("📊 Admin Dashboard")

    try:
        df = fetch_records()
    except Exception as e:
        st.error(f"Load data error: {e}")
        st.stop()

    if not df.empty:
        st.metric("Total Consents", len(df))

        display_df = df.drop(
            columns=["patient_signature", "doctor_signature", "nurse_signature"],
            errors="ignore",
        )
        st.dataframe(display_df, use_container_width=True)

        csv = display_df.to_csv(index=False).encode("utf-8-sig")
        st.download_button("⬇️ Export CSV", csv, "consents.csv", "text/csv")

        st.subheader("Procedure Count")
        st.bar_chart(df["procedure"].value_counts())
    else:
        st.info("No data")
