import streamlit as st
import pandas as pd
import xml.etree.ElementTree as ET
from io import BytesIO
import datetime
import pdfplumber
import pikepdf
import tempfile
import re

st.set_page_config(page_title="Repotic Automation Engine", page_icon="💼", layout="wide")

# Custom Styling
st.markdown("""
    <style>
    .main-title { font-size: 28px; font-weight: bold; color: #1E3A8A; margin-bottom: 20px; }
    </style>
""", unsafe_allow_html=True)

# ---------------------------------------------------------
# SMART PDF PARSER ENGINE (FIXES DIFFERENT BANK FORMATS)
# ---------------------------------------------------------
def process_real_pdf(uploaded_file, password=""):
    try:
        # File ko temporary save karna padta hai pdfplumber ke liye
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as temp_file:
            temp_file.write(uploaded_file.read())
            temp_pdf_path = temp_file.name

        all_rows = []
        
        # Open PDF (handles password if provided)
        with pdfplumber.open(temp_pdf_path, password=password) as pdf:
            for page in pdf.pages:
                table = page.extract_table()
                if table:
                    all_rows.extend(table)
                    
        if not all_rows:
            return pd.DataFrame(), "PDF mein koi table nahi mili ya image based PDF hai."

        # Convert to DataFrame
        df = pd.DataFrame(all_rows)
        
        # Pehli row ko header banate hain (Cleaning empty rows)
        df = df.dropna(how='all')
        df.columns = df.iloc[0].astype(str).str.lower().str.strip() # Sab small letters mein
        df = df[1:].reset_index(drop=True)

        # --- DYNAMIC COLUMN MAPPING (Isse Har Bank Fix Hoga) ---
        standard_df = pd.DataFrame()

        # 1. Date Format Matcher
        date_cols = [c for c in df.columns if any(k in c for k in ['date', 'txn', 'value'])]
        standard_df['Date'] = df[date_cols[0]] if date_cols else "01-01-2026"

        # 2. Narration / Description Matcher
        desc_cols = [c for c in df.columns if any(k in c for k in ['narration', 'description', 'particulars', 'remarks', 'details'])]
        standard_df['Narration'] = df[desc_cols[0]] if desc_cols else "Unknown Transaction"

        # 3. Ref / Cheque Matcher
        ref_cols = [c for c in df.columns if any(k in c for k in ['ref', 'chq', 'cheque', 'reference'])]
        standard_df['Reference/Chq'] = df[ref_cols[0]] if ref_cols else ""

        # 4. Debit / Withdrawal Matcher
        dr_cols = [c for c in df.columns if any(k in c for k in ['debit', 'dr', 'withdrawal', 'out'])]
        standard_df['Withdrawal (Dr)'] = df[dr_cols[0]] if dr_cols else 0.0

        # 5. Credit / Deposit Matcher
        cr_cols = [c for c in df.columns if any(k in c for k in ['credit', 'cr', 'deposit', 'in'])]
        standard_df['Deposit (Cr)'] = df[cr_cols[0]] if cr_cols else 0.0

        # 6. Balance Matcher
        bal_cols = [c for c in df.columns if 'balance' in c]
        standard_df['Balance'] = df[bal_cols[0]] if bal_cols else 0.0

        # Data Cleaning (Remove newlines, commas from numbers)
        standard_df = standard_df.replace('\n', ' ', regex=True)
        standard_df['Withdrawal (Dr)'] = pd.to_numeric(standard_df['Withdrawal (Dr)'].astype(str).str.replace(',', ''), errors='coerce').fillna(0)
        standard_df['Deposit (Cr)'] = pd.to_numeric(standard_df['Deposit (Cr)'].astype(str).str.replace(',', ''), errors='coerce').fillna(0)
        
        # Remove empty or irrelevant rows (where both Cr and Dr are 0)
        standard_df = standard_df[(standard_df['Withdrawal (Dr)'] > 0) | (standard_df['Deposit (Cr)'] > 0)]

        # Auto-Suggest Tally Ledger based on keywords
        def suggest_ledger(narration):
            narration = str(narration).lower()
            if 'amazon' in narration: return 'Amazon Sales Ledger'
            if 'flipkart' in narration: return 'Flipkart Sales Ledger'
            if 'upi' in narration or 'bharatpe' in narration: return 'UPI Retail Sales'
            if 'chrg' in narration or 'fee' in narration: return 'Bank Charges'
            if 'salary' in narration: return 'Salary A/c'
            return 'Suspense A/c' # Agar samajh na aaye toh Suspense mein dalo

        standard_df['Suggested Tally Ledger'] = standard_df['Narration'].apply(suggest_ledger)

        return standard_df, "Success"

    except pikepdf.PasswordError:
        return pd.DataFrame(), "Password galat hai ya PDF locked hai."
    except Exception as e:
        return pd.DataFrame(), f"Error: {str(e)}"

# ---------------------------------------------------------
# TALLY XML CONVERTER
# ---------------------------------------------------------
def convert_to_tally_xml(df):
    envelope = ET.Element("ENVELOPE")
    header = ET.SubElement(envelope, "HEADER")
    tallyrequest = ET.SubElement(header, "TALLYREQUEST")
    tallyrequest.text = "Import Data"
    
    body = ET.SubElement(envelope, "BODY")
    importdata = ET.SubElement(body, "IMPORTDATA")
    requestdesc = ET.SubElement(importdata, "REQUESTDESC")
    reportname = ET.SubElement(requestdesc, "REPORTNAME")
    reportname.text = "Vouchers"
    
    requestdata = ET.SubElement(importdata, "REQUESTDATA")
    
    for idx, row in df.iterrows():
        tallymessage = ET.SubElement(requestdata, "TALLYMESSAGE", {"VCHTYPE": "Journal"})
        voucher = ET.SubElement(tallymessage, "VOUCHER", {"VTYPE": "Journal", "ACTION": "Create"})
        
        # Try to parse date safely
        try:
            date_str = str(row['Date']).split(' ')[0] # Clean time if any
            date_obj = pd.to_datetime(date_str, dayfirst=True)
            vdate_str = date_obj.strftime("%Y%m%d")
        except:
            vdate_str = "20260401" # Default fallback
            
        vdate = ET.SubElement(voucher, "DATE")
        vdate.text = vdate_str
        
        vtype = ET.SubElement(voucher, "VOUCHERTYPENAME")
        vtype.text = "Receipt" if row['Deposit (Cr)'] > 0 else "Payment"
        
        narration = ET.SubElement(voucher, "NARRATION")
        narration.text = f"{row['Narration']} Ref: {row.get('Reference/Chq', '')}"
        
        all_ledger_entries_1 = ET.SubElement(voucher, "ALLLEDGERENTRIES.LIST")
        ledger_name_1 = ET.SubElement(all_ledger_entries_1, "LEDGERNAME")
        ledger_name_1.text = "Primary Bank A/c"
        amount_1 = ET.SubElement(all_ledger_entries_1, "AMOUNT")
        amount_1.text = str(-row['Deposit (Cr)']) if row['Deposit (Cr)'] > 0 else str(row['Withdrawal (Dr)'])
        
        all_ledger_entries_2 = ET.SubElement(voucher, "ALLLEDGERENTRIES.LIST")
        ledger_name_2 = ET.SubElement(all_ledger_entries_2, "LEDGERNAME")
        ledger_name_2.text = row['Suggested Tally Ledger']
        amount_2 = ET.SubElement(all_ledger_entries_2, "AMOUNT")
        amount_2.text = str(row['Deposit (Cr)']) if row['Deposit (Cr)'] > 0 else str(-row['Withdrawal (Dr)'])

    return ET.tostring(envelope, encoding="utf-8")


# ---------------------------------------------------------
# UI START
# ---------------------------------------------------------
st.sidebar.title("Repotic Engine v1.0")
menu = st.sidebar.radio("Navigation", ["🏦 Bank Statement Converter"])

if menu == "🏦 Bank Statement Converter":
    st.markdown('<div class="main-title">AI Bank Statement Parser (Multi-Bank Format)</div>', unsafe_allow_html=True)
    st.markdown("Yeh engine column keywords ('Particulars', 'Credit', 'Dr') ke basis par kisi bhi bank ka format auto-detect karta hai.")
    
    uploaded_file = st.file_uploader("Choose a Bank Statement PDF file", type=["pdf"])
    pdf_password = st.text_input("🔑 Enter PDF Password (if protected)", type="password")
    
    if uploaded_file is not None:
        if st.button("🚀 Process Statement"):
            with st.spinner('AI Parsing PDF and standardizing columns...'):
                df, status = process_real_pdf(uploaded_file, pdf_password)
                
            if status == "Success" and not df.empty:
                st.success("✅ Extracted & Standardized Successfully!")
                edited_df = st.data_editor(df, use_container_width=True)
                
                col1, col2 = st.columns(2)
                with col1:
                    output_excel = BytesIO()
                    with pd.ExcelWriter(output_excel, engine='xlsxwriter') as writer:
                        edited_df.to_excel(writer, index=False, sheet_name='Bank_Statement')
                    st.download_button("📥 Download Excel", output_excel.getvalue(), "Converted_Bank_Data.xlsx", "application/vnd.ms-excel", use_container_width=True)
                    
                with col2:
                    xml_data = convert_to_tally_xml(edited_df)
                    st.download_button("📥 Download Tally XML", xml_data, "Tally_Import.xml", "application/xml", use_container_width=True)
            else:
                st.error(f"❌ Failed to parse: {status}")
