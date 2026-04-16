
import pandas as pd
import json
import os

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..')
INPUT_FILE = os.path.join(PROJECT_ROOT, "data", "live_eauction_data.jsonl")
OUTPUT_FILE = os.path.join(PROJECT_ROOT, "data", "live_eauction_data.xlsx")

def convert():
    if not os.path.exists(INPUT_FILE):
        print(f"Error: {INPUT_FILE} not found.")
        return

    print(f"Reading {INPUT_FILE}...")
    data = []
    with open(INPUT_FILE, 'r', encoding='utf-8') as f:
        for line in f:
            try:
                data.append(json.loads(line))
            except: pass
            
    if not data:
        print("No data found to convert.")
        return

    print(f"Converting {len(data)} rows to DataFrame...")
    df = pd.DataFrame(data)
    
    # Reorder columns as per user priority if possible
    priority = ["Auction ID", "Bank Name", "Reserve Price", "EMD", "Title", "City", "State", "Description", "Downloads", "URL"]
    cols = list(df.columns)
    new_cols = []
    
    # Add priority cols first if they exist
    for p in priority:
        if p in cols:
            new_cols.append(p)
            cols.remove(p)
            
    # Add rest
    new_cols.extend(cols)
    df = df[new_cols]

    print(f"Saving to {OUTPUT_FILE}...")
    df.to_excel(OUTPUT_FILE, index=False)
    print("Done!")

if __name__ == "__main__":
    convert()
