import time
import requests
import json
import os
from docling.document_converter import DocumentConverter
from supabase import create_client, Client

# 1. Setup Environment Connections
SUPABASE_URL = "YOUR_SUPABASE_URL"
SUPABASE_KEY = "YOUR_SUPABASE_ANON_KEY"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Initialize Docling Layout Engine
doc_converter = DocumentConverter()

def listen_for_phone_uploads():
    # Poll the storage bucket for new form images
    try:
        files = supabase.storage.from_('incoming_forms').list()
    except Exception as e:
        print(f"Connection error to Supabase: {e}")
        return

    for file in files:
        file_name = file['name']
        if file_name == '.emptyFolderPlaceholder':
            continue
        
        print(f"\n[NEW CAPTURE] Found phone image upload: {file_name}")
        process_pipeline(file_name)

def process_pipeline(file_name):
    local_temp_path = f"./temp_{file_name}"
    
    try:
        # Step A: Download photo from cloud bucket down onto spare PC hardware
        print("Downloading form from secure storage queue...")
        with open(local_temp_path, "wb") as f:
            res = supabase.storage.from_('incoming_forms').download(file_name)
            f.write(res)

        # Step B: Execute Docling local layout/structural analysis
        print("Stage 1: Running Docling structural layout conversion...")
        conversion_result = doc_converter.convert(local_temp_path)
        markdown_layout = conversion_result.document.export_to_markdown()

        # Step C: Pipe clean text structure directly into Qwen 2.5 via local Ollama port
        print("Stage 2: Passing layout payload to Qwen via local Ollama...")
        system_prompt = (
            "You are a strict data parsing engine. Read this Markdown text extracted from a form. "
            "Convert it into a valid JSON object with these keys: 'applicant_name', 'date_of_birth', and 'form_type'. "
            "Do not write preamble, summaries, or backticks. Return raw JSON syntax only."
        )
        
        ollama_payload = {
            "model": "qwen2.5-vl:3b", # Ensure this matches your downloaded tag exactly
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"Document Text Structure:\n\n{markdown_layout}"}
            ],
            "stream": False,
            "format": "json"
        }
        
        ollama_res = requests.post("http://localhost:11434/api/chat", json=ollama_payload, timeout=60)
        ai_text = ollama_res.json()['message']['content']
        parsed_json = json.loads(ai_text)

        # Step D: Save perfect structural payload back up to your tracking database
        print("Stage 3: Storing structural JSON data back into Supabase database...")
        supabase.table("document_pipeline").insert({
            "file_name": file_name,
            "extracted_payload": parsed_json
        }).execute()

        print(f"[SUCCESS] Form processing complete for {file_name}. Logged to database.")

    except Exception as e:
        print(f"[PIPELINE CRASH] Failed processing: {e}")
        
    finally:
        # Clean up local temp file and purge bucket so it won't loop process
        if os.path.exists(local_temp_path):
            os.remove(local_temp_path)
        try:
            supabase.storage.from_('incoming_forms').remove([file_name])
        except Exception:
            pass

if __name__ == "__main__":
    print("=== Secure On-Premises Docling + Qwen 2.5 Pipeline Online ===")
    while True:
        listen_for_phone_uploads()
        time.sleep(3) # Scan your queue bucket every 3 seconds