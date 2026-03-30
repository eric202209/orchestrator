#!/usr/bin/env python3
"""Extract text from PDF using PyMuPDF and save to downloads/"""

import fitz  # PyMuPDF
import os
from pathlib import Path

def extract_pdf_text(pdf_path):
    """Extract all text from a PDF file."""
    try:
        doc = fitz.open(pdf_path)
        text = ""
        
        for page_num in range(len(doc)):
            page = doc[page_num]
            text += f"\n{'='*50}\nPAGE {page_num + 1}\n{'='*50}\n"
            text += page.get_text()
        
        doc.close()
        return text
    
    except Exception as e:
        return f"Error: {str(e)}"

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python3 extract_pdf.py <pdf_file>")
        sys.exit(1)
    
    pdf_path = sys.argv[1]
    
    # Check file exists
    if not os.path.exists(pdf_path):
        print(f"File not found: {pdf_path}")
        sys.exit(1)
    
    # Extract text
    extracted_text = extract_pdf_text(pdf_path)
    
    # Save to downloads folder with .txt extension
    downloads_dir = Path("/root/.openclaw/workspace/downloads")
    downloads_dir.mkdir(exist_ok=True)
    
    base_name = os.path.basename(pdf_path).replace(".pdf", "")
    output_file = downloads_dir / f"{base_name}_extracted.txt"
    
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(f"# Extracted from: {os.path.basename(pdf_path)}\n")
        f.write(f"# Date: 2026-03-30\n")
        f.write(extracted_text)
    
    print(f"✅ Text extracted and saved to: {output_file}")
    print(f"\n--- Preview (first 500 chars) ---\n")
    print(extracted_text[:500])
