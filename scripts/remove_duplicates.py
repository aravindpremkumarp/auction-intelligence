
import json
import os
import shutil
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scrapers'))
from fast_eauctions_scraper import convert_jsonl_to_csv # Reuse CSV conversion

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..')

def clean_data():
    input_file = os.path.join(PROJECT_ROOT, "data", "live_eauction_data.jsonl")
    if not os.path.exists(input_file):
        print(f"Error: {input_file} not found.")
        return

    print(f"Reading {input_file}...")
    data = []
    seen_urls = set()
    duplicates_count = 0
    unique_data = []

    with open(input_file, 'r', encoding='utf-8') as f:
        for line in f:
            try:
                item = json.loads(line)
                url = item.get("URL")
                if url:
                    if url in seen_urls:
                        duplicates_count += 1
                        continue
                    seen_urls.add(url)
                    unique_data.append(item)
            except: pass
    
    total_lines = duplicates_count + len(unique_data)
    print(f"Total Lines Scanned: {total_lines}")
    print(f"Unique Entries: {len(unique_data)}")
    print(f"Duplicates Found: {duplicates_count}")

    if duplicates_count > 0:
        print("Removing duplicates...")
        # Backup
        shutil.copy(input_file, input_file + ".bak")
        print(f"Backup created at {input_file}.bak")
        
        with open(input_file, 'w', encoding='utf-8') as f:
            for item in unique_data:
                f.write(json.dumps(item) + "\n")
        print("JSONL Cleared.")
        
        # Update CSV
        print("Regenerating CSV...")
        convert_jsonl_to_csv()
    else:
        print("No duplicates to remove.")

if __name__ == "__main__":
    clean_data()
