# streamlit_app.py
# Thai E-Consent App
# Streamlit + PostgreSQL + Mobile Camera Signature Capture + PDF + Admin Dashboard
# FIXES/FEATURES:
# - no cached psycopg2 connection; fresh short-lived DB connections
# - no streamlit-drawable-canvas dependency
# - uses st.camera_input for patient/doctor/nurse paper-signature capture on mobile
# - expanded block menu: ankle, brachial, popliteal, cervical, sciatic, other
#
# requirements.txt:
# streamlit
# pandas
# psycopg2-binary
# reportlab
# pytz
# pillow
#
# Streamlit Cloud secrets.toml example:
# DATABASE_URL = "postgresql://postgres.PROJECT_ID:PASSWORD@aws-0-ap-southeast-1.pooler.supabase.com:6543/postgres?sslmode=require"
# Optional, if you upload a Thai font file into your repo:
# THAI_FONT_PATH = "fonts/NotoSansThai-Regular.ttf"

import base64
import html
import io
import os
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd
import psycopg2
import pytz
import streamlit as st
from PIL import Image, ImageOps
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


def get_conn():
    """Create a short-lived PostgreSQL connection. Do not cache psycopg2 connections in Streamlit."""
    return psycopg2.connect(get_database_url(), connect_timeout=10)


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


try:
    init_db()
except Exception as e:
    st.error(f"PostgreSQL connection/table error: {e}")
    st.stop()

# ---------------- PDF FONT ----------------
def setup_pdf_font():
    """
    Register TH Sarabun New for clean Thai PDF output.

    Priority:
    1) Font uploaded in the Streamlit sidebar during the current session
    2) THAI_FONT_PATH in Streamlit secrets / environment
    3) THSarabunNew.ttf in app root
    4) fonts/THSarabunNew.ttf

    Returns:
        tuple[str | None, str | None]: (reportlab_font_name, actual_font_path)
    """
    st.sidebar.markdown("### PDF Thai font")
    uploaded_font = st.sidebar.file_uploader(
        "Upload THSarabunNew.ttf if Thai PDF looks wrong",
        type=["ttf"],
        key="thai_font_upload",
    )

    candidates = []

    if uploaded_font is not None:
        tmp_font_path = Path(tempfile.gettempdir()) / "THSarabunNew_uploaded.ttf"
        tmp_font_path.write_bytes(uploaded_font.getvalue())
        candidates.append(str(tmp_font_path))

    secret_font = st.secrets.get("THAI_FONT_PATH", None) or os.getenv("THAI_FONT_PATH")
    if secret_font:
        candidates.append(secret_font)

    candidates.extend([
        "THSarabunNew.ttf",
        "./THSarabunNew.ttf",
        "fonts/THSarabunNew.ttf",
        "./fonts/THSarabunNew.ttf",
    ])

    for font_path in candidates:
        if font_path and Path(font_path).exists():
            try:
                pdfmetrics.registerFont(TTFont("THSarabunNew", font_path))
                st.sidebar.success(f"Using Thai PDF font: {Path(font_path).name}")
                return "THSarabunNew", str(font_path)
            except Exception as e:
                st.sidebar.warning(f"Font found but could not be loaded: {font_path} ({e})")

    st.sidebar.error(
        "THSarabunNew.ttf was not found. PDF generation is disabled until the font is uploaded "
        "or placed in the app root / fonts folder."
    )
    return None, None


PDF_FONT, PDF_FONT_PATH = setup_pdf_font()

# ---------------- HELPERS ----------------
def now_bkk():
    return datetime.now(BKK).strftime("%Y-%m-%d %H:%M:%S")


def image_to_base64_png(img: Image.Image, max_width: int = 900) -> str:
    """Normalize, resize and encode image as base64 PNG for PostgreSQL TEXT storage."""
    img = ImageOps.exif_transpose(img)
    img = img.convert("RGB")

    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, max(1, int(img.height * ratio))))

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def camera_signature(label: str, key: str, required: bool = False):
    """
    Capture a signature from paper using mobile camera.
    Streamlit camera_input opens the device camera on mobile browser and returns one captured frame.
    """
    st.markdown(f"### {label}")
    st.caption("ให้เซ็นบนกระดาษ แล้วใช้กล้องมือถือถ่ายเฉพาะบริเวณลายเซ็นให้ชัดเจน")

    photo = st.camera_input("ถ่ายภาพลายเซ็นจากกระดาษ", key=key)
    if photo is None:
        if required:
            st.caption("จำเป็นต้องมีลายเซ็นนี้ก่อนบันทึก")
        return None

    try:
        img = Image.open(photo)
        sig64 = image_to_base64_png(img)
        preview_img = Image.open(io.BytesIO(base64.b64decode(sig64)))
        st.image(preview_img, caption=label, width=320)
        return sig64
    except Exception as e:
        st.error(f"ไม่สามารถอ่านภาพลายเซ็นได้: {e}")
        return None


def decode_sig_to_tempfile(sig64: str):
    data = base64.b64decode(sig64)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".png")
    tmp.write(data)
    tmp.close()
    return tmp.name


def procedure_information(procedure: str) -> str:
    """Thai plain-language consent information by procedure."""
    info = {
        "Ankle Block": """
การฉีดยาชาบริเวณข้อเท้าเพื่อทำแผลหรือหัตถการบริเวณเท้า

ความเสี่ยงที่อาจเกิดขึ้น:
- เจ็บบริเวณที่ฉีด
- ชาชั่วคราวหรืออ่อนแรงชั่วคราว
- เลือดออกหรือช้ำ
- ติดเชื้อ
- แพ้ยา
- เส้นประสาทระคายเคืองหรือบาดเจ็บ พบน้อย
""",
        "Brachial Block": """
การฉีดยาชาบริเวณเส้นประสาทแขนเพื่อทำแผลหรือหัตถการบริเวณแขน/มือ

ความเสี่ยงที่อาจเกิดขึ้น:
- เจ็บบริเวณที่ฉีด
- ชาหรืออ่อนแรงชั่วคราว
- เลือดออกหรือช้ำ
- แพ้ยา
- เส้นประสาทบาดเจ็บ พบน้อย
""",
        "Popliteal Block": """
การฉีดยาชาบริเวณหลังเข่าเพื่อระงับความรู้สึกบริเวณขาส่วนล่าง/เท้า

ความเสี่ยงที่อาจเกิดขึ้น:
- เจ็บบริเวณที่ฉีด
- ชาหรืออ่อนแรงชั่วคราวของขาหรือเท้า
- เลือดออกหรือช้ำ
- ติดเชื้อ
- แพ้ยา
- เส้นประสาทระคายเคืองหรือบาดเจ็บ พบน้อย
""",
        "Cervical Block": """
การฉีดยาชาบริเวณคอ/เส้นประสาทบริเวณคอ ตามข้อบ่งชี้ทางการแพทย์

ความเสี่ยงที่อาจเกิดขึ้น:
- เจ็บบริเวณที่ฉีด
- เลือดออกหรือช้ำ
- ติดเชื้อ
- แพ้ยา
- ชาหรืออ่อนแรงชั่วคราว
- เส้นประสาทระคายเคืองหรือบาดเจ็บ พบน้อย
- อาการผิดปกติบริเวณคอหรือการหายใจ ควรแจ้งแพทย์ทันทีหากเกิดขึ้น
""",
        "Sciatic Block": """
การฉีดยาชาบริเวณเส้นประสาทไซอาติกเพื่อระงับความรู้สึกบริเวณขา/เท้า

ความเสี่ยงที่อาจเกิดขึ้น:
- เจ็บบริเวณที่ฉีด
- ชาหรืออ่อนแรงชั่วคราวของขาหรือเท้า
- เลือดออกหรือช้ำ
- ติดเชื้อ
- แพ้ยา
- เส้นประสาทระคายเคืองหรือบาดเจ็บ พบน้อย
""",
        "Others": """
หัตถการบล็อกเส้นประสาทหรือฉีดยาชาเฉพาะที่อื่น ๆ ตามที่แพทย์อธิบาย

ความเสี่ยงทั่วไปที่อาจเกิดขึ้น:
- เจ็บบริเวณที่ฉีด
- ชาหรืออ่อนแรงชั่วคราว
- เลือดออกหรือช้ำ
- ติดเชื้อ
- แพ้ยา
- เส้นประสาทระคายเคืองหรือบาดเจ็บ พบน้อย
""",
    }
    return info.get(procedure, info["Others"])


def pdf_safe_text(value) -> str:
    """Escape text for ReportLab Paragraph while preserving Thai text and line breaks."""
    if value is None:
        return ""
    return html.escape(str(value)).replace("\n", "<br/>")


def thai_paragraph(value, style):
    return Paragraph(pdf_safe_text(value), style)


def create_pdf(record: dict):
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
    if not PDF_FONT:
        raise RuntimeError(
            "THSarabunNew.ttf not loaded. Please upload THSarabunNew.ttf in the sidebar "
            "or place it in the app root / fonts folder."
        )

    normal = ParagraphStyle(
        "ThaiNormal",
        parent=styles["Normal"],
        fontName=PDF_FONT,
        fontSize=16,
        leading=22,
        wordWrap="CJK",
        splitLongWords=False,
    )
    title = ParagraphStyle(
        "ThaiTitle",
        parent=styles["Title"],
        fontName=PDF_FONT,
        fontSize=24,
        leading=30,
        wordWrap="CJK",
    )

    story = [thai_paragraph("ใบยินยอมทำหัตถการ", title), Spacer(1, 12)]

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
            story.append(thai_paragraph(f"{labels.get(key, key)}: {record.get(key)}", normal))
            story.append(Spacer(1, 6))

    story.append(Spacer(1, 8))
    story.append(thai_paragraph("ข้อมูลที่ได้รับการอธิบาย", normal))
    for line in procedure_information(record.get("procedure", "Others")).strip().split("\n"):
        if line.strip():
            story.append(thai_paragraph(line.strip(), normal))

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
            story.append(thai_paragraph(sig_label, normal))
            story.append(RLImage(imgfile, width=7 * cm, height=3.5 * cm))

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
        block_options = [
            "Ankle Block",
            "Brachial Block",
            "Popliteal Block",
            "Cervical Block",
            "Sciatic Block",
            "Others",
        ]
        procedure_choice = st.selectbox("หัตถการ / Block", block_options)
        other_procedure = ""
        if procedure_choice == "Others":
            other_procedure = st.text_input("ระบุหัตถการอื่น ๆ", placeholder="เช่น Digital block, Local infiltration, Field block")

    procedure = other_procedure.strip() if procedure_choice == "Others" and other_procedure.strip() else procedure_choice

    st.markdown("---")
    st.info(procedure_information(procedure_choice))

    agree = st.checkbox("ข้าพเจ้าได้รับคำอธิบาย เข้าใจประโยชน์ ความเสี่ยง ทางเลือก และยินยอมให้ทำหัตถการ")

    st.markdown("---")
    st.subheader("ถ่ายภาพลายเซ็นจากกระดาษ")
    st.info(
        "วิธีใช้บนมือถือ: ให้ผู้ป่วย แพทย์ และพยาบาลเซ็นบนกระดาษ แล้วกดปุ่มกล้องแต่ละช่องเพื่อถ่ายภาพลายเซ็น "
        "ระบบจะบันทึกภาพลง PostgreSQL และใส่ใน PDF อัตโนมัติ"
    )

    sig_col1, sig_col2, sig_col3 = st.columns(3)
    with sig_col1:
        patient_sig = camera_signature("ผู้ป่วยเซ็นชื่อ", "patient_signature_camera", required=True)
    with sig_col2:
        doctor_sig = camera_signature("แพทย์เซ็นชื่อ", "doctor_signature_camera")
    with sig_col3:
        nurse_sig = camera_signature("พยาบาลพยานเซ็นชื่อ", "nurse_signature_camera")

    st.markdown("---")
    if st.button("💾 Save", type="primary"):
        if not patient_id.strip() or not patient_name.strip():
            st.warning("กรุณากรอก HN / เลขบัตร และชื่อผู้ป่วย")
            st.stop()
        if not agree:
            st.warning("กรุณาติ๊กยินยอมก่อนบันทึก")
            st.stop()
        if procedure_choice == "Others" and not other_procedure.strip():
            st.warning("กรุณาระบุหัตถการอื่น ๆ")
            st.stop()
        if not patient_sig:
            st.warning("กรุณาถ่ายภาพลายเซ็นผู้ป่วย")
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



